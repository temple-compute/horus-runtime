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
import contextvars
import json
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.substitution import (
    placeholder_names,
    placeholder_roots,
)
from horus_builtin.task.horus_task import (
    _UNHASHABLE,
    HorusTask,
    TaskFingerprint,
)
from horus_builtin.workflow.scheduler import TargetPool, execute_task
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.iterable import IterableArtifact
from horus_runtime.core.task.base import MAX_TARGET_LOSS_RETRIES, BaseTask
from horus_runtime.core.task.exceptions import (
    TargetLostError,
    TaskExecutionError,
    exception_leaves,
)
from horus_runtime.core.task.status import SkipReason, TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow, EdgeSource
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

# The drop event lists this many dropped items, each error cut to
# _ERROR_CHARS: a large map dropping many items must not flood the log.
_DROPS_LISTED = 20
_ERROR_CHARS = 500

# The highest exit code a process can report.
_MAX_EXIT_CODE = 255


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
    and completes the map, unless every item was dropped; the clone stays
    FAILED, which is how a dropped item shows in the run. Only meant for
    maps whose items are independent.
    """

    chain: bool = False
    """Start each item once every chained input has that item."""

    zip: list[MapOver] = Field(default_factory=list)
    """
    Inputs paired by item: slot X of each is bound to clone X as ``$<as>``.
    """

    drop_exit_codes: list[int] = Field(default_factory=list)
    """
    Exit codes that drop the item instead of failing it, whatever
    :attr:`on_item_failure` says. The clone ends SKIPPED with
    ``skip_reason: dropped``.
    """

    _dropped_slots: list[str] = PrivateAttr(default_factory=list)
    """Slots dropped by the current execution."""

    _staged_dirs: list[str] = PrivateAttr(default_factory=list)
    """The output root and slot dirs of the current execution."""

    _staged_generation: int = PrivateAttr(default=0)
    """Machine generation the inputs and dirs were last staged on."""

    _fan_out: (
        tuple[
            list[BaseTask],
            Callable[[BaseTask], Coroutine[Any, Any, None]],
            contextvars.Context,
        ]
        | None
    ) = PrivateAttr(default=None)
    """
    The clones, how to run one and the context to run it in, while the first
    wave runs.
    """

    _early_reruns: dict[int, asyncio.Task[None]] = PrivateAttr(
        default_factory=dict
    )
    """Re-runs of lost slots :meth:`_restage` started, by clone index."""

    _input_digests: dict[str, str] = PrivateAttr(default_factory=dict)
    """The digests of this task's inputs from its last fingerprint."""

    @property
    def _over_artifact(self) -> IterableArtifact:
        """
        Returns the input artifact to iterate over.
        """
        return self._iterable_input(self.over.input_id)

    def _iterable_input(self, input_id: str) -> IterableArtifact:
        """
        The input *input_id*, which must be an iterable artifact.
        """
        input_artifact = next(
            (inp for inp in self.inputs if inp.id == input_id), None
        )
        if input_artifact is None:
            raise MapConfigurationError(
                _("Input artifact '%(id)s' not found in task inputs.")
                % {"id": input_id}
            )
        if not isinstance(input_artifact, IterableArtifact):
            raise MapConfigurationError(
                _("Input artifact '%(id)s' is not iterable.")
                % {"id": input_id}
            )

        return input_artifact

    @model_validator(mode="after")
    def validate_input_artifact_iterable(self) -> "MapTask":
        """
        Validates that the input artifacts specified by `over` and `zip` are
        IterableArtifacts.
        """
        for over in (self.over, *self.zip):
            self._iterable_input(over.input_id)

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
    def validate_zip_bindings(self) -> "MapTask":
        """
        Validates that each zip entry binds a free id, and that no input is
        iterated twice.
        """
        taken = {a.id for a in (*self.inputs, *self.outputs)}
        bound = {self.over.item_id}
        paired = {self.over.input_id}
        for entry in self.zip:
            item = None
            if entry.item_id in taken or entry.item_id in bound:
                item = entry.item_id
            elif entry.input_id in paired:
                item = entry.input_id
            if item is not None:
                raise MapConfigurationError(
                    _("Map task '%(id)s' binds '%(item)s' twice.")
                    % {"id": self.id, "item": item}
                )
            bound.add(entry.item_id)
            paired.add(entry.input_id)
        return self

    @model_validator(mode="after")
    def validate_chained_body(self) -> "MapTask":
        """
        Validates that a chained command body reads each streamed input one
        item at a time: the whole folder is not there while it streams.
        ``${task.resources...}``, ``${task.id}`` and ``${task.name}`` are
        fine; ``${task.inputs}`` and ``${task.outputs}`` are not.
        """
        if not self.chain or not isinstance(self.runtime, CommandRuntime):
            return self
        names = placeholder_names(self._body_text())
        roots = {name.split(".")[0] for name in names}
        for over in (self.over, *self.zip):
            if over.input_id in roots:
                self._raise_chained_read(over.input_id, over.item_id)
        for name in sorted(names):
            head = ".".join(name.split(".")[:2])
            if head in ("task.inputs", "task.outputs"):
                self._raise_chained_read(head, self.over.item_id)
        return self

    def _raise_chained_read(self, input_id: str, item_id: str) -> None:
        """Raise the V7 error for *input_id*, pointing at *item_id*."""
        raise MapConfigurationError(
            _(
                "Map task '%(id)s' is chained, so it cannot read all of "
                "'%(input)s'. Use '$%(as)s' for this item."
            )
            % {"id": self.id, "input": input_id, "as": item_id}
        )

    @model_validator(mode="after")
    def validate_drop_exit_codes(self) -> "MapTask":
        """
        Validates that each drop code is an exit code a command can return
        on failure.
        """
        for code in self.drop_exit_codes:
            if code == 0:
                raise MapConfigurationError(
                    _(
                        "Map task '%(id)s' lists exit code %(code)d to drop "
                        "items, but 0 means success."
                    )
                    % {"id": self.id, "code": code}
                )
            if not 1 <= code <= _MAX_EXIT_CODE:
                raise MapConfigurationError(
                    _(
                        "Map task '%(id)s' lists exit code %(code)d to drop "
                        "items. Use a code from 1 to 255."
                    )
                    % {"id": self.id, "code": code}
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
        # Clone ids repeat across a map retry: a stale record must not read
        # as this run's output.
        for clone in clones:
            wf.leases.forget(clone)
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
                self.target,
                artifact,
                report=False,
                digest=self._input_digests.get(artifact.id),
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
            self._run_item,
            partial(
                execute_task,
                wf,
                source_map=source_map,
                pool=TargetPool(self.max_concurrency),
                placement=wf.placement,
            ),
        )
        results: list[BaseException | None]
        # Re-runs start from a ready callback; they must not inherit the
        # target's provisioning context.
        self._fan_out = (clones, run_clone, contextvars.copy_context())
        self._early_reruns = {}
        try:
            if (
                wf.failure_policy == "continue"
                or self.on_item_failure == "drop"
                or self.drop_exit_codes
            ):
                # Clones are independent: one failing must not cancel the
                # rest.
                results = await asyncio.gather(
                    *map(run_clone, clones), return_exceptions=True
                )
            else:
                async with asyncio.TaskGroup() as tg:
                    for clone in clones:
                        tg.create_task(run_clone(clone))
                results = [None] * len(clones)
            self._fan_out = None
            early = await asyncio.gather(
                *self._early_reruns.values(), return_exceptions=True
            )
        finally:
            self._fan_out = None
            for rerun in self._early_reruns.values():
                rerun.cancel()
            await asyncio.gather(
                *self._early_reruns.values(), return_exceptions=True
            )
        for index, result in zip(self._early_reruns, early, strict=True):
            results[index] = result
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

    async def _run_item(
        self,
        run_task: Callable[[BaseTask], Awaitable[None]],
        clone: BaseTask,
    ) -> None:
        """
        Run *clone*. When it exits with one of :attr:`drop_exit_codes`, mark
        it dropped; it still raises, so :meth:`_settle` drops its slot.
        """
        try:
            await run_task(clone)
        except Exception as exc:
            code = self._drop_code(exc)
            if code is not None:
                # shortcut: set after the run raised, so a snapshot can show
                # FAILED for one push; mark it inside the run if that matters.
                clone.status = TaskStatus.SKIPPED
                clone.skip_reason = SkipReason.DROPPED
                clone.error = _("Dropped: exit code %(code)d") % {"code": code}
            raise

    def _drop_code(self, exc: BaseException) -> int | None:
        """
        The exit code that drops the item *exc* failed, when every leaf of
        *exc* exited with one of :attr:`drop_exit_codes`; else ``None``.
        """
        codes = [
            getattr(leaf, "exit_code", None) for leaf in exception_leaves(exc)
        ]
        if codes and all(code in self.drop_exit_codes for code in codes):
            return codes[0]
        return None

    def _droppable(self, exc: BaseException) -> bool:
        """Whether a clone failing with *exc* drops its item."""
        if isinstance(exc, asyncio.CancelledError):
            return False
        return (
            self._drop_code(exc) is not None or self.on_item_failure == "drop"
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
            # shortcut: inputs a loop placed in place are not transferred,
            # so they cannot come back; restage them once loops need it.
            if any(artifact.pinned for artifact in self.inputs):
                raise RuntimeError(
                    _("The inputs of map task '%(id)s' are pinned.")
                    % {"id": self.id}
                )
            paths = [artifact.path for artifact in self.inputs]
            wf.leases.repull(self)
            try:
                await wf.transfer_artifacts(self)
            finally:
                wf.leases.inputs_transferred(self)
            if [artifact.path for artifact in self.inputs] != paths:
                raise RuntimeError(
                    _("The inputs of map task '%(id)s' moved.")
                    % {"id": self.id}
                )
            for directory in self._staged_dirs:
                await self.target.mkdir(directory)
            self._start_lost_reruns(wf)
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

    def _start_lost_reruns(self, wf: BaseWorkflow) -> None:
        """
        Start now, not after the first wave, the re-runs of the finished
        clones lost with the old generation. :meth:`_rerun_lost` stays the
        safety net.
        """
        if self._fan_out is None or self.target.keeps_data_on_release:
            return
        clones, run_clone, context = self._fan_out
        for index, clone in enumerate(clones):
            if (
                index not in self._early_reruns
                and clone.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)
                and wf.leases.output_lost(
                    clone.id, clone.outputs[0].id, self.target
                )
                and self._take_rerun(clone)
            ):
                self._early_reruns[index] = asyncio.create_task(
                    run_clone(clone), context=context.copy()
                )

    @staticmethod
    def _take_rerun(clone: BaseTask) -> bool:
        """
        Spend *clone*'s one re-run of a lost slot and say so; ``False`` once
        it is spent. The clone leaves a terminal status, so the event says
        why.
        """
        if clone._target_loss_retries >= MAX_TARGET_LOSS_RETRIES:  # noqa: SLF001
            return False
        clone._target_loss_retries += 1  # noqa: SLF001
        HorusContext.get_context().bus.emit(
            HorusTaskEvent(
                task_id=clone.id,
                task_name=clone.name,
                message=_(
                    "Output of %(task_name)s lost with its machine; "
                    "running it again."
                )
                % {"task_name": clone.name},
            )
        )
        return True

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
                and wf.leases.output_lost(
                    clone.id, clone.outputs[0].id, target
                )
            ]
            rerun: list[int] = []
            for index in lost:
                clone = clones[index]
                if self._take_rerun(clone):
                    rerun.append(index)
                    continue
                error = TargetLostError(
                    _("The output of %(task_name)s was lost with its machine.")
                    % {"task_name": clone.name}
                )
                clone._finish(TaskStatus.FAILED, error)  # noqa: SLF001
                results[index] = error
                HorusContext.get_context().bus.emit(
                    HorusTaskEvent(
                        task_id=clone.id,
                        task_name=clone.name,
                        message=str(error),
                    )
                )
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
        if a clone failed and is not droppable, or if every item was
        dropped. Then remove each dropped slot, so nothing downstream sees
        it, and report the drop.
        """
        errors = [r for r in results if isinstance(r, BaseException)]
        # A cancelled clone is a cancel, not a failure: never group it.
        for error in errors:
            if isinstance(error, asyncio.CancelledError):
                raise error
        if not errors:
            return
        fatal = [error for error in errors if not self._droppable(error)]
        if fatal:
            raise BaseExceptionGroup(
                _("%(n)d of %(total)d map clone(s) failed.")
                % {"n": len(fatal), "total": len(clones)},
                fatal,
            )
        if len(errors) == len(clones):
            raise TaskExecutionError(
                _("All %(n)d items were dropped in %(name)s.")
                % {"n": len(clones), "name": self.name}
            )

        lines: list[str] = []
        dropped: list[dict[str, str]] = []
        for (slot, item), clone, result in zip(
            slots.items(), clones, results, strict=True
        ):
            if result is None:
                continue
            await self.target.remove(str(root / slot))
            self._dropped_slots.append(slot)
            summary = (clone.error or "")[:_ERROR_CHARS]
            code = self._drop_code(result)
            dropped.append(
                {
                    "slot": slot,
                    "item": Path(item.path).name,
                    "reason": summary
                    if code is None
                    else _("exit code %(code)d") % {"code": code},
                }
            )
            if len(lines) < _DROPS_LISTED:
                lines.append(f"{slot} ({Path(item.path).name}): {summary}")
        if len(dropped) > _DROPS_LISTED:
            lines.append(
                _("+%(n)d more") % {"n": len(dropped) - _DROPS_LISTED}
            )

        # The message is capped; `data` lists every drop for the UI.
        HorusContext.get_context().bus.emit(
            HorusTaskEvent(
                task_id=self.id,
                task_name=self.name,
                level="WARNING",
                message=_("Map %(name)s dropped %(n)d of %(total)d item(s):")
                % {"name": self.name, "n": len(dropped), "total": len(clones)}
                + "\n"
                + "\n".join(lines),
                data={"map": {"dropped": dropped, "upstream_dropped": {}}},
            )
        )

    def _config_extra(self) -> dict[str, Any]:
        """
        ``zip`` and ``drop_exit_codes`` change what the map outputs; left
        out when unset, so the hash of a map without them is unchanged.
        """
        extra: dict[str, Any] = {}
        if self.zip:
            extra["zip"] = [
                entry.model_dump(mode="json") for entry in self.zip
            ]
        if self.drop_exit_codes:
            extra["drop_exit_codes"] = self.drop_exit_codes
        return extra

    async def _fingerprint(
        self, input_digests: Mapping[str, str | None] | None = None
    ) -> TaskFingerprint:
        """
        The fingerprint, keeping its input digests: ``_run`` takes it just
        before :meth:`_execute`, so the clones reuse them for the inputs
        they share instead of each hashing them again.
        """
        fingerprint = await super()._fingerprint(input_digests)
        self._input_digests = {
            key: value
            for key, value in fingerprint.inputs.items()
            if value != _UNHASHABLE
        }
        return fingerprint

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
        return bool(
            placeholder_roots(self._body_text()) & {artifact_id, "task"}
        )

    def _body_text(self) -> str:
        """The runtime and executor, as the text their placeholders are in."""
        return json.dumps(
            [
                self.runtime.model_dump(mode="json"),
                self.executor.model_dump(mode="json"),
            ]
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
            "error",
            "started_at",
            "finished_at",
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

        # The shared inputs are the copies this task just digested (a
        # digest does not depend on the path): the clone hashes its item
        # only. The scheduler replaces this when it digested them itself.
        clone._staged_digests = dict(self._input_digests)  # noqa: SLF001

        # Register this clone's item as a side artifact, so the upload
        # middleware persists it to S3 under the clone's own task id and the
        # UI can inspect exactly what this one run of the body received. The
        # item is an input transfer, not an output of the body, so without
        # this it would never be uploaded at all.
        clone.side_artifacts.append(
            item.model_copy(update={"id": f"{clone.id}_item"})
        )

        return clone
