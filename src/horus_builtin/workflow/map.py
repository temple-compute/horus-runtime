#
# horus-runtime
# Copyright (C) 2026 Temple Compute
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""
horus_map: an ordinary task that runs its own body once per item of an
iterable input.

The task declares one iterable input (named by ``over``) and one folder
output. At execution it enumerates that input into items (see
:class:`~horus_runtime.core.artifact.iterable.IterableArtifact`), clones
itself once per item as a plain ``horus_task`` -- same runtime, executor and
other inputs, with the collection input replaced by the item -- and runs
every clone concurrently. Each clone owns a numbered slot directory under
the folder output, so the folder gathers the whole fan-out for free.

Everything else is an ordinary node on the canvas: producing the collection
is whatever task writes the iterable artifact, and folding the slots back
into one value is whatever task consumes the folder. The map itself only
fans out.
"""

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import model_validator

from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.scheduler import TargetPool, execute_task
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.iterable import IterableArtifact
from horus_runtime.core.workflow.base import EdgeSource
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger


class MapConfigurationError(WorkflowError):
    """Raised when a ``horus_map`` task is misconfigured."""


class MapTask(HorusTask):
    """
    Runs this task's own body once per item of :attr:`over`, each clone
    writing into its own slot directory under the single folder output.
    """

    kind: str = "horus_map"
    kind_name: ClassVar[str] = "Map"
    kind_description: ClassVar[str] = _(
        "Runs this task's body once per item of an iterable input, each run "
        "writing into its own slot of a single folder output."
    )

    over: str
    """
    Id of the input artifact to iterate over. Must be an IterableArtifact.
    """

    max_concurrency: int | None = None
    """Upper bound on clones dispatched at once; ``None`` means unbounded."""

    @property
    def _over_artifact(self) -> IterableArtifact:
        """
        Returns the input artifact to iterate over.
        """
        input_artifact = next(
            (inp for inp in self.inputs if inp.id == self.over), None
        )
        if input_artifact is None:
            raise MapConfigurationError(
                _("Input artifact '%(id)s' not found in task inputs.")
                % {"id": self.over}
            )
        if not isinstance(input_artifact, IterableArtifact):
            raise MapConfigurationError(
                _("Input artifact '%(id)s' is not iterable.")
                % {"id": self.over}
            )

        return input_artifact

    @model_validator(mode="after")
    def validate_input_artifact_iterable(self) -> "MapTask":
        """
        Validates that the input artifact specified by `over` is an
        IterableArtifact.
        """
        # Obtain the input artifact to iterate over
        _ = self._over_artifact

        return self

    @model_validator(mode="after")
    def validate_output_artifact_folder(self) -> "MapTask":
        """
        Validates that the output artifact is a FolderArtifact.

        The folder is what makes the fan-out addressable downstream: each
        clone owns one slot directory inside it.
        """
        if len(self.outputs) != 1:
            raise MapConfigurationError(
                _("Map task '%(id)s' must have exactly one output.")
                % {"id": self.id}
            )
        output_artifact = self.outputs[0]
        if not isinstance(output_artifact, FolderArtifact):
            raise MapConfigurationError(
                _("Output artifact '%(id)s' must be a folder.")
                % {"id": output_artifact.id}
            )
        return self

    async def _execute(self) -> None:
        """
        Enumerate the collection, build one clone per item, and run them all.
        """
        wf = self.workflow
        if wf is None:
            raise MapConfigurationError(
                _("Map task '%(id)s' must run inside a workflow.")
                % {"id": self.id}
            )

        items = await self._over_artifact.items(self.target)
        root = Path(self.target.path_on_target(self.outputs[0]))
        await self.target.mkdir(str(root))

        # Zero-padded so the slots sort the same lexically and numerically,
        # both on the filesystem and in the DAG.
        width = max(1, len(str(len(items) - 1)))
        clones: list[HorusTask] = []
        for index, item in enumerate(items):
            slot = f"{index:0{width}d}"
            slot_root = root / slot
            await self.target.mkdir(str(slot_root))
            clones.append(self._clone(slot, item, slot_root))

        # Ordering-only edges (no artifact ids, so `transfer=False`): they
        # exist purely to bring the clones into the scheduler's
        # trigger-reachable scope, never to source a transfer.
        wf.expand(
            tasks=list(clones),
            edges=[
                WorkflowEdge(source=self.id, target=clone.id)
                for clone in clones
            ],
        )

        # Every clone input is already materialized on `self.target` (either
        # the item, or a copy of one of this task's own already-transferred
        # inputs), so it doubles as its own transfer origin: a co-located
        # clone target no-ops through BaseTransferStrategy.transfer's
        # same-location_id shortcut, a non-co-located one gets a real
        # transfer via the registered strategy. Nothing needs `pinned`.
        source_map: dict[tuple[str, str], EdgeSource] = {
            (clone.id, artifact.id): EdgeSource(self.target, artifact)
            for clone in clones
            for artifact in clone.inputs
        }

        pool = TargetPool(self.max_concurrency)
        async with asyncio.TaskGroup() as tg:
            for clone in clones:
                tg.create_task(
                    execute_task(
                        wf,
                        clone,
                        source_map=source_map,
                        pool=pool,
                        placement=wf.placement,
                    )
                )

        for clone in clones:
            self.side_artifacts.extend(clone.side_artifacts)

        horus_logger.log.debug(
            _("Map task '%(id)s' ran %(n)d clone(s).")
            % {"id": self.id, "n": len(clones)}
        )

    def _clone(
        self, slot: str, item: BaseArtifact, slot_root: Path
    ) -> HorusTask:
        """
        Clone independent task instances for each item of the collection.
        """
        # The item stands in for the collection under the very same input id,
        # so `$<over>` in the body renders the item's own on-target path and
        # the body is written exactly as if it handled a single element.
        item = item.model_copy(update={"id": self.over})

        # This task's own remaining inputs are already materialized on
        # `self.target` (its transfer step ran before `_execute`), but their
        # `declared_path` still holds whatever relative value they were
        # authored with. Freeze the current absolute path onto the copy.
        inputs: list[BaseArtifact] = []
        for artifact in self.inputs:
            if artifact.id == self.over:
                inputs.append(item)
                continue
            copy = artifact.model_copy(deep=True)
            copy.declared_path = copy.path
            inputs.append(copy)

        # The clone's output is its slot directory, so a body writing into
        # `$<output>` lands under the map's folder with no path juggling.
        output = self.outputs[0].model_copy(deep=True)
        output.path = slot_root
        output.declared_path = slot_root

        # Exclude fields specifically overriden in the clone.
        exclude_fields = {"id", "name", "inputs", "outputs", "target"}

        return HorusTask(
            **self.model_dump(exclude=exclude_fields),
            id=f"{self.id}[{slot}]",
            name=f"{self.name}[{slot}]",
            inputs=inputs,
            outputs=[output],
            target=self.target.idle_copy(),
        )
