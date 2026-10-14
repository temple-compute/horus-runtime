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
import json
import tempfile
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.artifact.json import JSONArtifact
from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.substitution import placeholder_roots
from horus_builtin.task.horus_task import HorusTask, TaskFingerprint
from horus_builtin.workflow.scheduler import TargetPool, execute_task
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.iterable import IterableArtifact
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow, EdgeSource
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


class DroppedItem(BaseModel):
    """
    One item a :class:`MapTask` dropped because its clone failed.
    """

    slot: str
    """Name of the slot (and of the clone, ``<map>[<slot>]``)."""

    item: str
    """Entry name of the item the clone received."""

    error: str | None
    """The failed clone's error summary."""


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
    """
    Upper bound on clones dispatched at once. ``None`` (the default) applies
    a conservative built-in cap (see
    ``horus_builtin.workflow.scheduler.DEFAULT_MAX_CONCURRENCY``) instead of
    dispatching every clone at once.
    """

    on_item_failure: Literal["fail", "drop"] = "fail"
    """
    What a failed clone means for the map. ``"fail"`` (the default) fails
    the map. ``"drop"`` removes the clone's slot from the output folder,
    records it in :attr:`dropped_items` and completes the map, unless every
    clone failed. Only meant for maps whose items are independent.
    """

    dropped_items: list[DroppedItem] = Field(default_factory=list)
    """Items dropped by the last execution (``on_item_failure="drop"``)."""

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

        dropped_id = f"{self.id}_dropped"
        self.dropped_items = []
        self.side_artifacts = [
            a for a in self.side_artifacts if a.id != dropped_id
        ]

        items = await self._over_artifact.items(self.target)
        root = Path(self.target.path_on_target(self.outputs[0]))
        await self.target.mkdir(str(root))

        # Over another map's output, keep its slot names: a slot it dropped
        # leaves a gap instead of shifting every later item to a new slot
        # (and a new fingerprint). Otherwise zero-padded indices, so the
        # slots sort the same lexically and numerically.
        width = max(1, len(str(len(items) - 1)))
        inherit = self._over_is_map_output(wf)
        slots: dict[str, BaseArtifact] = {
            (_item_name(item) if inherit else f"{index:0{width}d}"): item
            for index, item in enumerate(items)
        }
        clones: list[BaseTask] = []
        for slot, item in slots.items():
            slot_root = root / slot
            await self.target.mkdir(str(slot_root))
            clones.append(self._clone(slot, item, slot_root))

        # Slots from an earlier, larger expansion (or one padded to another
        # width) would otherwise survive next to this run's. Surviving slots
        # are kept: their contents are what lets an unchanged clone skip.
        for entry in await self.target.list_dir(str(root)):
            if entry.name not in slots:
                await self.target.remove(entry.path)

        # Ordering-only edges (no artifact ids, so `transfer=False`): they
        # exist purely to bring the clones into the scheduler's
        # trigger-reachable scope, never to source a transfer.
        wf.expand(
            tasks=clones,
            edges=[
                WorkflowEdge(source=self.id, target=clone.id)
                for clone in clones
            ],
            owner=self.id,
        )

        # Every clone input is already materialized on `self.target` (either
        # the item, or a copy of one of this task's own already-transferred
        # inputs), so it doubles as its own transfer origin: a co-located
        # clone target no-ops through BaseTransferStrategy.transfer's
        # same-location_id shortcut, a non-co-located one gets a real
        # transfer via the registered strategy. Nothing needs `pinned`.
        # These hand-offs are not workflow edges, so they emit no
        # TransferEvents (one per clone x input would flood the bus).
        source_map: dict[tuple[str, str], EdgeSource] = {
            (clone.id, artifact.id): EdgeSource(
                self.target, artifact, report=False
            )
            for clone in clones
            for artifact in clone.inputs
        }

        # The clones are this task's real work, and they bypass its executor,
        # which is what flips a task from CONFIGURING to RUNNING; without
        # this the map reports "configuring" for its whole fan-out.
        self.status = TaskStatus.RUNNING

        pool = TargetPool(self.max_concurrency)
        runs = [
            execute_task(
                wf,
                clone,
                source_map=source_map,
                pool=pool,
                placement=wf.placement,
            )
            for clone in clones
        ]
        if wf.failure_policy == "continue" or self.on_item_failure == "drop":
            # Clones are independent: one failing must not cancel the rest.
            # The map still fails once they have all finished, unless it
            # drops failed items and at least one succeeded.
            results = await asyncio.gather(*runs, return_exceptions=True)
            errors = [r for r in results if isinstance(r, BaseException)]
            # A cancelled clone is a cancel, not a failure: never group it.
            for error in errors:
                if isinstance(error, asyncio.CancelledError):
                    raise error
            if errors and (
                self.on_item_failure == "fail" or len(errors) == len(clones)
            ):
                raise BaseExceptionGroup(
                    _("%(n)d of %(total)d map clone(s) failed.")
                    % {"n": len(errors), "total": len(clones)},
                    errors,
                )
            if errors:
                await self._drop(
                    root,
                    [
                        (slot, item, clone)
                        for (slot, item), clone, result in zip(
                            slots.items(), clones, results, strict=True
                        )
                        if isinstance(result, BaseException)
                    ],
                    len(clones),
                )
        else:
            async with asyncio.TaskGroup() as tg:
                for run in runs:
                    tg.create_task(run)

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

    def _over_is_map_output(self, wf: BaseWorkflow) -> bool:
        """
        Whether the collection this map iterates is another map's output,
        judged from the workflow edge feeding :attr:`over`.
        """
        source = next(
            (
                e.source
                for e in wf.edges
                if e.target == self.id and e.target_input == self.over.input_id
            ),
            None,
        )
        return any(isinstance(t, MapTask) and t.id == source for t in wf.tasks)

    async def _drop(
        self,
        root: Path,
        failed: list[tuple[str, BaseArtifact, BaseTask]],
        total: int,
    ) -> None:
        """
        Remove each failed clone's slot, so nothing downstream sees it, and
        record it in :attr:`dropped_items` and a ``<id>_dropped`` side
        artifact.
        """
        for slot, item, clone in failed:
            await self.target.remove(str(root / slot))
            self.dropped_items.append(
                DroppedItem(
                    slot=slot, item=_item_name(item), error=clone.error
                )
            )

        report = JSONArtifact(
            id=f"{self.id}_dropped",
            path=Path(tempfile.mkdtemp(prefix="horus-map-"))
            / f"{self.id}_dropped.json",
        )
        report.write([d.model_dump(mode="json") for d in self.dropped_items])
        self.side_artifacts.append(report)

        HorusContext.get_context().bus.emit(
            HorusTaskEvent(
                task_id=self.id,
                task_name=self.name,
                message=_(
                    "Map %(name)s dropped %(n)d of %(total)d item(s): "
                    "%(slots)s"
                )
                % {
                    "name": self.name,
                    "n": len(failed),
                    "total": total,
                    "slots": ", ".join(d.slot for d in self.dropped_items),
                },
            )
        )

    async def _write_manifest(self, fingerprint: TaskFingerprint) -> None:
        """
        Record *fingerprint*, unless items were dropped: then remove any
        earlier manifest instead, so the next run re-enters the map and
        retries the dropped items (the others skip on their own manifests).
        """
        if not self.dropped_items:
            await super()._write_manifest(fingerprint)
            return
        path = self._manifest_path()
        if path is not None:
            await self.target.remove(path)

    def _body_may_read(self, artifact_id: str) -> bool:
        """
        Whether the body can read *artifact_id*. Only a command body is
        known not to: it reaches artifacts solely through ``$`` placeholders
        (``${task...}`` counts as reaching all of them). Any other runtime,
        e.g. a Python function given artifacts by parameter name, may.
        """
        if not isinstance(self.runtime, CommandRuntime):
            return True
        text = json.dumps(
            [
                self.runtime.model_dump(mode="json"),
                self.executor.model_dump(mode="json"),
            ]
        )
        return bool(placeholder_roots(text) & {artifact_id, "task"})

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
            "on_item_failure",
            "dropped_items",
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

        # The item already fingerprints this clone's slice of the
        # collection. Hashing the whole collection as well made every clone
        # read every sibling's data (quadratic in the map's size) and rerun
        # when any other item changed. Keep it only when the body names it.
        if not self._body_may_read(self.over.input_id):
            clone.unfingerprinted_inputs = [self.over.input_id]

        # Register this clone's item as a side artifact, so the upload
        # middleware persists it to S3 under the clone's own task id and the
        # UI can inspect exactly what this one run of the body received. The
        # item is an input transfer, not an output of the body, so without
        # this it would never be uploaded at all.
        clone.side_artifacts.append(
            item.model_copy(update={"id": f"{clone.id}_item"})
        )

        return clone


def _item_name(item: BaseArtifact) -> str:
    """
    The entry name of a collection item (its id after the collection's).
    """
    return item.id.rpartition(":")[2]
