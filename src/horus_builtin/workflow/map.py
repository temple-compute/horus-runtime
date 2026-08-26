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
output.
"""

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.scheduler import TargetPool, execute_task
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.iterable import IterableArtifact
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.workflow.base import EdgeSource
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger


class MapConfigurationError(WorkflowError):
    """Raised when a ``horus_map`` task is misconfigured."""


class MapOver(BaseModel):
    """
    What a :class:`MapTask` iterates, and the id each item is bound to.
    """

    model_config = ConfigDict(populate_by_name=True)

    input_id: str
    """Id of this task's own input holding the collection."""

    item_id: str = Field(alias="as")
    """
    Id the per-item artifact is created under on each clone. It is a new
    artifact, so the collection itself stays addressable under
    :attr:`input_id` in the body.
    """


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

    over: MapOver
    """
    The input artifact to iterate over (must be an IterableArtifact), and
    the id each item is bound to on the clone that receives it.
    """

    max_concurrency: int | None = None
    """Upper bound on clones dispatched at once; ``None`` means unbounded."""

    @property
    def _over_artifact(self) -> IterableArtifact:
        """
        Returns the input artifact to iterate over.
        """
        input_artifact = next(
            (inp for inp in self.inputs if inp.id == self.over.input_id), None
        )
        if input_artifact is None:
            raise MapConfigurationError(
                _("Input artifact '%(id)s' not found in task inputs.")
                % {"id": self.over.input_id}
            )
        if not isinstance(input_artifact, IterableArtifact):
            raise MapConfigurationError(
                _("Input artifact '%(id)s' is not iterable.")
                % {"id": self.over.input_id}
            )

        return input_artifact

    @model_validator(mode="after")
    def validate_input_artifact_iterable(self) -> "MapTask":
        """
        Validates that the input artifact specified by `over` is an
        IterableArtifact.
        """
        # Obtain the input artifact to iterate over
        _unused = self._over_artifact

        return self

    @model_validator(mode="after")
    def validate_item_id_is_free(self) -> "MapTask":
        """
        Validates that the per-item id does not collide with a declared port.
        """
        taken = {a.id for a in (*self.inputs, *self.outputs)}
        if self.over.item_id in taken:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' binds each item to '%(item)s', which "
                    "is already the id of one of its own inputs or outputs."
                )
                % {"id": self.id, "item": self.over.item_id}
            )
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
        clones: list[BaseTask] = []
        for index, item in enumerate(items):
            slot = f"{index:0{width}d}"
            slot_root = root / slot
            await self.target.mkdir(str(slot_root))
            clones.append(self._clone(slot, item, slot_root))

        # Ordering-only edges (no artifact ids, so `transfer=False`): they
        # exist purely to bring the clones into the scheduler's
        # trigger-reachable scope, never to source a transfer.
        wf.expand(
            tasks=clones,
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

        # Deliberately NOT merging the clones' side artifacts into this
        # task's own list: each clone's upload middleware registers them
        # under the clone's own task id, and a re-upload here (same artifact
        # ids, different task) would repoint every reference at this map,
        # erasing the per-clone attribution the UI's clone browser reads.
        # The aggregate view is the backend's job: listing a task's side
        # products includes those of its `id[slot]` descendants.

        horus_logger.log.debug(
            _("Map task '%(id)s' ran %(n)d clone(s).")
            % {"id": self.id, "n": len(clones)}
        )

    def _clone(
        self, slot: str, item: BaseArtifact, slot_root: Path
    ) -> BaseTask:
        """
        One independent clone of this task for *slot*, bound to *item* and
        rooted at *slot_root*.

        A clone is a plain :class:`~horus_builtin.task.horus_task.HorusTask`,
        so it runs the body instead of mapping over it again.
        """
        # This task's own inputs are already materialized on `self.target`
        # (its transfer step ran before `_execute`), but their
        # `declared_path` still holds whatever relative value they were
        # authored with. Freeze the current absolute path onto the copy.
        inputs: list[BaseArtifact] = []
        for artifact in self.inputs:
            copy = artifact.model_copy(deep=True)
            copy.declared_path = copy.path
            inputs.append(copy)

        # The item joins them as a new artifact under `over.as`, so the body
        # addresses this one element through `$<as>` while the collection it
        # came from stays addressable through `$<input_id>`.
        inputs.append(item.model_copy(update={"id": self.over.item_id}))

        # The clone's output is its slot directory, so a body writing into
        # `$<output>` lands under the map's folder with no path juggling.
        output = self.outputs[0].model_copy(deep=True)
        output.path = slot_root
        output.declared_path = slot_root

        # Excluded from the dump: what the clone sets for itself, the
        # map-only fields (keeping `kind: horus_map` would make a stored
        # clone reload as a MapTask with no `over`), and this run's state.
        exclude_fields = {
            "id",
            "name",
            "inputs",
            "outputs",
            "target",
            "kind",
            "over",
            "max_concurrency",
            "side_artifacts",
            "status",
            "skip_reason",
            "runs",
        }

        clone = HorusTask(
            **self.model_dump(exclude=exclude_fields),
            id=f"{self.id}[{slot}]",
            name=f"{self.name}[{slot}]",
            inputs=inputs,
            outputs=[output],
            target=self.target.idle_copy(),
        )

        # Register this clone's item as a side artifact, so the upload
        # middleware persists it to S3 under the clone's own task id and the
        # UI can inspect exactly what this one run of the body received. The
        # item is an input transfer, not an output of the body, so without
        # this it would never be uploaded at all.
        clone.side_artifacts.append(
            item.model_copy(update={"id": f"{clone.id}_item"})
        )

        return clone
