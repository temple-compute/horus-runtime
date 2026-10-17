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
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.substitution import placeholder_roots
from horus_builtin.task.horus_task import HorusTask, TaskFingerprint
from horus_builtin.workflow.scheduler import TargetPool, execute_task
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.iterable import IterableArtifact
from horus_runtime.core.task.base import MAX_TARGET_LOSS_RETRIES, BaseTask
from horus_runtime.core.task.exceptions import TargetLostError
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow, EdgeSource
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

# The drop event lists this many dropped items, each error cut to
# _ERROR_CHARS: a large map dropping many items must not flood the log.
_DROPS_LISTED = 20
_ERROR_CHARS = 500


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
    dispatches_children: ClassVar[bool] = True

    over: MapOver
    """
    The input artifact to iterate over (must be an IterableArtifact), and
    the id each item is bound to on the clone that receives it.
    """

    max_concurrency: int | None = Field(default=None, ge=0)
    """
    Upper bound on clones dispatched at once. ``None`` (the default) applies
    a conservative built-in cap (see
    ``horus_builtin.workflow.scheduler.DEFAULT_MAX_CONCURRENCY``) instead of
    dispatching every clone at once; ``0`` dispatches every clone at once.
    """

    on_item_failure: Literal["fail", "drop"] = "fail"
    """
    What a failed clone means for the map. ``"fail"`` (the default) fails
    the map. ``"drop"`` removes the clone's slot from the output folder
    and completes the map, unless every clone failed; the clone stays
    FAILED, which is how a dropped item shows in the run. Only meant for
    maps whose items are independent.
    """

    _dropped_slots: list[str] = PrivateAttr(default_factory=list)
    """Slots dropped by the current execution (``on_item_failure="drop"``)."""

    _staged_dirs: list[str] = PrivateAttr(default_factory=list)
    """The output root and slot dirs of the current execution."""

    _staged_generation: int = PrivateAttr(default=0)
    """Machine generation the inputs and dirs were last staged on."""

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

        self._dropped_slots = []

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
            (Path(item.path).name if inherit else f"{index:0{width}d}"): item
            for index, item in enumerate(items)
        }
        clones: list[BaseTask] = []
        for slot, item in slots.items():
            slot_root = root / slot
            await self.target.mkdir(str(slot_root))
            clones.append(self._clone(slot, item, slot_root))
        self._staged_dirs = [str(root), *(str(root / slot) for slot in slots)]
        self._staged_generation = self.target.generation

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

        # A replaced machine comes back empty: stage it again before the
        # pending clones run on it.
        self.target.on_ready(self._restage)

        run_clone = partial(
            execute_task,
            wf,
            source_map=source_map,
            pool=TargetPool(self.max_concurrency),
            placement=wf.placement,
        )
        results: list[BaseException | None]
        if wf.failure_policy == "continue" or self.on_item_failure == "drop":
            # Clones are independent: one failing must not cancel the rest.
            results = await asyncio.gather(
                *map(run_clone, clones), return_exceptions=True
            )
        else:
            async with asyncio.TaskGroup() as tg:
                for clone in clones:
                    tg.create_task(run_clone(clone))
            results = [None] * len(clones)
        await self._rerun_lost(clones, results, run_clone)
        await self._settle(root, slots, clones, results)

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

    async def _restage(self) -> None:
        """
        Put this task's inputs and slot dirs back on a new generation of its
        machine. The clones froze the input paths, so they must not move.
        """
        wf = self.workflow
        generation = self.target.generation
        if (
            wf is None
            or self.status != TaskStatus.RUNNING
            or generation == self._staged_generation
        ):
            return
        # Set first: a callback registered by an earlier execution also runs.
        self._staged_generation = generation
        try:
            paths = [artifact.path for artifact in self.inputs]
            await wf.transfer_artifacts(self)
            if [artifact.path for artifact in self.inputs] != paths:
                raise RuntimeError(
                    _("The inputs of map task '%(id)s' moved.")
                    % {"id": self.id}
                )
            for directory in self._staged_dirs:
                await self.target.mkdir(directory)
        except Exception as exc:
            HorusContext.get_context().bus.emit(
                HorusTaskEvent(
                    task_id=self.id,
                    task_name=self.name,
                    message=_(
                        "Map %(name)s could not restage its inputs on the "
                        "new machine: %(error)s"
                    )
                    % {"name": self.name, "error": exc},
                )
            )
            raise

    async def _rerun_lost(
        self,
        clones: list[BaseTask],
        results: list[BaseException | None],
        run_clone: Callable[[BaseTask], Awaitable[None]],
    ) -> None:
        """
        Run again the succeeded clones whose slot was lost with an earlier
        generation of the machine, once each; past that budget, record them
        in *results* as failed with :class:`TargetLostError`.
        """
        wf = self.workflow
        target = self.target
        if wf is None or not target.releasable or target.keeps_data_on_release:
            return
        while True:
            lost = [
                index
                for index, clone in enumerate(clones)
                if results[index] is None
                and wf.leases.produced_generation(
                    clone.id, clone.outputs[0].id
                )
                != target.generation
            ]
            rerun: list[int] = []
            for index in lost:
                clone = clones[index]
                if clone._target_loss_retries < MAX_TARGET_LOSS_RETRIES:  # noqa: SLF001
                    clone._target_loss_retries += 1  # noqa: SLF001
                    rerun.append(index)
                    continue
                error = TargetLostError(
                    _("The output of %(task_name)s was lost with its machine.")
                    % {"task_name": clone.name}
                )
                clone._finish(TaskStatus.FAILED, error)  # noqa: SLF001
                results[index] = error
            if not rerun:
                return
            rerun_results = await asyncio.gather(
                *(run_clone(clones[index]) for index in rerun),
                return_exceptions=True,
            )
            for index, result in zip(rerun, rerun_results, strict=True):
                results[index] = result

    def _over_is_map_output(self, wf: BaseWorkflow) -> bool:
        """
        Whether the collection this map iterates is another map's output,
        judged from the workflow edge feeding :attr:`over`.
        """
        source = wf.cached_source_map(None)[1].get(
            (self.id, self.over.input_id)
        )
        return source is not None and any(
            isinstance(t, MapTask) and t.id == source.task_id for t in wf.tasks
        )

    async def _settle(
        self,
        root: Path,
        slots: dict[str, BaseArtifact],
        clones: list[BaseTask],
        results: list[BaseException | None],
    ) -> None:
        """
        Turn the finished clones' *results* into the map's outcome: raise
        if any clone failed, unless the map drops failed items and at least
        one succeeded. Then remove each failed clone's slot, so nothing
        downstream sees it, and report the drop.
        """
        errors = [r for r in results if isinstance(r, BaseException)]
        # A cancelled clone is a cancel, not a failure: never group it.
        for error in errors:
            if isinstance(error, asyncio.CancelledError):
                raise error
        if not errors:
            return
        if self.on_item_failure == "fail" or len(errors) == len(clones):
            raise BaseExceptionGroup(
                _("%(n)d of %(total)d map clone(s) failed.")
                % {"n": len(errors), "total": len(clones)},
                errors,
            )

        failed = [
            (slot, item, clone)
            for (slot, item), clone, result in zip(
                slots.items(), clones, results, strict=True
            )
            if isinstance(result, BaseException)
        ]
        lines: list[str] = []
        for slot, item, clone in failed:
            await self.target.remove(str(root / slot))
            self._dropped_slots.append(slot)
            if len(lines) < _DROPS_LISTED:
                summary = (clone.error or "")[:_ERROR_CHARS]
                lines.append(f"{slot} ({Path(item.path).name}): {summary}")
        if len(failed) > _DROPS_LISTED:
            lines.append(_("+%(n)d more") % {"n": len(failed) - _DROPS_LISTED})

        HorusContext.get_context().bus.emit(
            HorusTaskEvent(
                task_id=self.id,
                task_name=self.name,
                message=_("Map %(name)s dropped %(n)d of %(total)d item(s):")
                % {"name": self.name, "n": len(failed), "total": len(clones)}
                + "\n"
                + "\n".join(lines),
            )
        )

    async def _write_manifest(self, fingerprint: TaskFingerprint) -> None:
        """
        Record *fingerprint*, unless items were dropped: then remove any
        earlier manifest instead, so the next run re-enters the map and
        retries the dropped items (the others skip on their own manifests).
        """
        if self._dropped_slots:
            await self._remove_manifest()
        else:
            await super()._write_manifest(fingerprint)

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

        # Excluded from the dump: the map-only fields, what the clone sets
        # for itself (keeping `kind: horus_map` would make a stored clone
        # reload as a MapTask with no `over`), and this run's state.
        exclude_fields = (
            MapTask.model_fields.keys() - HorusTask.model_fields.keys()
        ) | {
            "id",
            "name",
            "inputs",
            "outputs",
            "target",
            "kind",
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
