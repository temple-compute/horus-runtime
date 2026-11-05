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
import shlex
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
from horus_runtime.core.artifact.store import CacheMode, compose_folder_digest
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import (
    MAX_TARGET_LOSS_RETRIES,
    BaseTask,
    InputDigests,
)
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

# An event's `data` lists at most this many slots per list, with the full
# count: a map of many items must not send MB-sized events.
_EVENT_ITEMS = 100

# The highest exit code a process can report.
_MAX_EXIT_CODE = 255

# Characters per `rm -rf` command line. Linux caps one argument at 128 KiB,
# and sshd passes the whole command as one (`$SHELL -c`).
_RM_CHARS = 32 * 1024


class MapConfigurationError(WorkflowError):
    """Raised when a ``horus_map`` task is misconfigured."""


@dataclass(frozen=True)
class _Item:
    """How one item of a map's stream settled."""

    state: Literal["ready", "dropped", "failed"]
    origin: str | None = None
    """Dropped: the id of the map the drop started in."""
    reason: str | None = None
    """Dropped: the exit code or the clone's error summary."""


class _ItemStream:
    """
    A map's items as its clones settle, so a chained consumer can start
    each item without waiting for the whole map. Futures carry
    :class:`_Item` values, never exceptions, and resolving a settled slot
    is a no-op.
    """

    def __init__(self) -> None:
        loop = asyncio.get_running_loop()
        self.opened: asyncio.Future[None] = loop.create_future()
        """Done once the slot list is known."""
        self.closed: asyncio.Future[bool] = loop.create_future()
        """Done once the producing run exited; ``True`` if it succeeded."""
        self.slots: dict[str, str] = {}
        """Slot to item label."""
        self.clones: dict[str, BaseTask] = {}
        """Slot to the producing clone."""
        self.target: BaseTarget | None = None
        """The producing map's target while it runs."""
        self.taken: set[str] = set()
        """Slots a consumer has asked for."""
        self._items: dict[str, asyncio.Future[_Item]] = {}

    def open(
        self,
        slots: dict[str, str],
        clones: dict[str, BaseTask],
        target: BaseTarget,
    ) -> None:
        """
        Publish the slot list. A retried run keeps it, with its own clones
        and machine: a slot no consumer asked for yet waits for that run.
        """
        if self.opened.done():
            self.clones = clones
            self.target = target
            for slot in slots:
                self.again(slot)
            return
        loop = asyncio.get_running_loop()
        self.slots = slots
        self.clones = clones
        self.target = target
        self._items = {slot: loop.create_future() for slot in slots}
        self.opened.set_result(None)

    def again(self, slot: str) -> None:
        """*slot* runs again: a consumer that has not asked for it waits."""
        future = self._items.get(slot)
        if future is not None and future.done() and slot not in self.taken:
            self._items[slot] = asyncio.get_running_loop().create_future()

    def _resolve(self, slot: str, item: _Item) -> None:
        future = self._items.get(slot)
        if future is not None and not future.done():
            future.set_result(item)

    def ready(self, slot: str) -> None:
        """The producing clone of *slot* succeeded or skipped complete."""
        self._resolve(slot, _Item("ready"))

    def dropped(self, slot: str, origin: str, reason: str | None) -> None:
        """*slot* was dropped, in the map *origin*."""
        self._resolve(slot, _Item("dropped", origin, reason))

    def failed(self, slot: str | None = None) -> None:
        """*slot*, or every slot not settled yet, will not arrive."""
        for key in list(self._items) if slot is None else [slot]:
            self._resolve(key, _Item("failed"))

    def close(self, *, ok: bool) -> None:
        """The producing run exited: fail every slot left."""
        self.failed()
        if not self.closed.done():
            self.closed.set_result(ok)

    async def wait(self, slot: str, *, owner: str, source: str) -> _Item:
        """How *slot* settled, once it has (R1 if it is not a slot)."""
        future = self._items.get(slot)
        if future is None:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' expected item %(slot)s from "
                    "'%(source)s', which has no such item."
                )
                % {"id": owner, "slot": slot, "source": source}
            )
        self.taken.add(slot)
        # Shielded: a cancelled waiter must not cancel the shared future.
        return await asyncio.shield(future)


async def _gap(origin: str) -> _Item:
    """A slot a paired input does not have: dropped in *origin*."""
    return _Item("dropped", origin)


async def _remove_slots(target: BaseTarget, paths: list[str]) -> None:
    """
    Remove the dropped slots *paths* on *target*: with its own ``remove``
    when it has one, else with as few ``rm -rf`` commands as fit a command
    line. A slot that stays is logged; it is still listed as dropped.
    """
    if type(target).remove is not BaseTarget.remove:
        for path in paths:
            try:
                await target.remove(path)
            except OSError as exc:
                horus_logger.log.warning(
                    _("Could not remove dropped slot %(path)s: %(error)s")
                    % {"path": path, "error": exc}
                )
        return
    chunks: list[list[str]] = []
    size = _RM_CHARS
    for path in map(shlex.quote, paths):
        if size + 1 + len(path) > _RM_CHARS:
            chunks.append([])
            size = len("rm -rf --")
        chunks[-1].append(path)
        size += 1 + len(path)
    for chunk in chunks:
        out = await target.run_command_sync("rm -rf -- " + " ".join(chunk))
        code = await out.wait()
        if code != 0:
            horus_logger.log.warning(
                _(
                    "Could not remove %(n)d dropped slot(s) "
                    "(exit code %(code)d): %(paths)s"
                )
                % {"n": len(chunk), "code": code, "paths": " ".join(chunk)}
            )


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
            Callable[[BaseTask], Coroutine[Any, Any, BaseException | None]],
            contextvars.Context,
        ]
        | None
    ) = PrivateAttr(default=None)
    """
    The clones, how to run one and the context to run it in, while the first
    wave runs.
    """

    _early_reruns: dict[int, asyncio.Task[BaseException | None]] = PrivateAttr(
        default_factory=dict
    )
    """Re-runs of lost slots :meth:`_restage` started, by clone index."""

    _input_digests: dict[str, str] = PrivateAttr(default_factory=dict)
    """The digests of this task's inputs from its last fingerprint."""

    _stream: _ItemStream | None = PrivateAttr(default=None)
    """This map's items for chained consumers, while it runs."""

    _chain_memo: (
        tuple[dict[tuple[str, str], EdgeSource], dict[str, "MapTask"]] | None
    ) = PrivateAttr(default=None)
    """The source map last seen, and the map feeding each chained input."""

    _source_streams: dict[str, _ItemStream] = PrivateAttr(default_factory=dict)
    """The stream of each chained input, as handed to the scheduler."""

    _upstream_dropped: dict[str, str] = PrivateAttr(default_factory=dict)
    """Slots dropped upstream in the current execution, to their origin."""

    _streamed_digests: dict[str, str | None] | None = PrivateAttr(default=None)
    """Final digests of the inputs this execution streamed, else ``None``."""

    _holding: bool = PrivateAttr(default=False)
    """Whether this execution keeps its sources' machines until it settles."""

    def stream_for_consumers(self) -> _ItemStream:
        """
        This map's item stream, created on first ask: a consumer can ask
        before this map runs, and the run reuses it.
        """
        stream = self._stream
        if (
            stream is None
            or stream.opened.get_loop() is not asyncio.get_running_loop()
        ):
            stream = self._stream = _ItemStream()
        return stream

    def stream_sources(
        self, source_map: dict[tuple[str, str], EdgeSource]
    ) -> dict[str, "asyncio.Future[None]"]:
        """
        A chained map may start once each map feeding its ``over`` and
        ``zip`` inputs has opened its stream.
        """
        if not self.chain:
            return {}
        sources = self._chain_sources(source_map)
        # Kept so the run reads the streams the scheduler saw open, even if
        # a source finishes (and resets) before then.
        self._source_streams = {
            input_id: source.stream_for_consumers()
            for input_id, source in sources.items()
        }
        return {
            sources[input_id].id: stream.opened
            for input_id, stream in self._source_streams.items()
        }

    def mark_skipped_complete(self) -> None:
        """
        Skipped whole: a stream a consumer asked for will never open, and
        one a retry kept open closes, its outputs complete.
        """
        stream = self._stream
        if stream is not None and stream.opened.done():
            for slot in stream.slots:
                stream.ready(slot)
            stream.close(ok=True)
        self._stream = None
        super().mark_skipped_complete()

    def _chain_sources(
        self, source_map: dict[tuple[str, str], EdgeSource]
    ) -> dict[str, "MapTask"]:
        """The map feeding each ``over`` and ``zip`` input, by input id."""
        memo = self._chain_memo
        if memo is not None and memo[0] is source_map:
            return memo[1]
        wf = self.workflow
        maps = {
            task.id: task
            for task in (wf.tasks if wf is not None else [])
            if isinstance(task, MapTask)
        }
        sources: dict[str, MapTask] = {}
        for entry in (self.over, *self.zip):
            source = source_map.get((self.id, entry.input_id))
            if source is not None and source.task_id in maps:
                sources[entry.input_id] = maps[source.task_id]
        self._chain_memo = (source_map, sources)
        return sources

    @property
    def streamed_inputs(self) -> frozenset[str]:
        """The chained inputs whose source map this run started on."""
        memo = self._chain_memo
        if not self._streaming_from or memo is None:
            return frozenset()
        return frozenset(
            input_id
            for input_id, source in memo[1].items()
            if source.id in self._streaming_from
        )

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
        ``${task.inputs}`` and ``${task.inputs.<streamed id>}`` are not
        fine; other ``${task...}`` names and inputs that do not stream are.
        """
        if not self.chain or not isinstance(self.runtime, CommandRuntime):
            return self
        names = placeholder_names(self._body_text())
        roots = {name.split(".")[0] for name in names}
        streamed = {
            over.input_id: over.item_id for over in (self.over, *self.zip)
        }
        for input_id, item_id in streamed.items():
            if input_id in roots:
                self._raise_chained_read(input_id, item_id)
        for name in sorted(names):
            if name == "task.inputs":
                self._raise_chained_read(name, self.over.item_id)
            if not name.startswith("task.inputs."):
                continue
            input_id = name.split(".")[2]
            if input_id in streamed:
                self._raise_chained_read(
                    "task.inputs." + input_id, streamed[input_id]
                )
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
        self._upstream_dropped = {}
        self._streamed_digests = None
        streams = {
            input_id: self._source_streams[input_id]
            for input_id in self.streamed_inputs
        }
        stream = self.stream_for_consumers()
        ok = keep = False
        try:
            await self._map_items(wf, stream, streams)
            ok = True
        except TargetLostError:
            # The scheduler runs this map again: its consumers keep waiting.
            keep = self._retry_on_target_loss
            raise
        finally:
            if not keep:
                stream.close(ok=ok)
                self._stream = None
                self._source_streams = {}
            if self._holding:
                self._holding = False
                wf.leases.inputs_transferred(self)

    async def _map_items(  # noqa: PLR0912, PLR0915
        self,
        wf: BaseWorkflow,
        stream: _ItemStream,
        streams: dict[str, _ItemStream],
    ) -> None:
        """
        The body of :meth:`_execute`. *stream* is this map's own; *streams*
        are those of the inputs it reads per item while their maps run.
        """
        over_stream = streams.get(self.over.input_id)
        slots: dict[str, BaseArtifact]
        if over_stream is not None:
            # The source is still running: its slot list, not its folder.
            labels = dict(over_stream.slots)
            slots = {
                slot: over_stream.clones[slot].outputs[0] for slot in labels
            }
        else:
            items = await self._over_artifact.items(self.target)
            # Over another map's output, keep its slot names: a slot it
            # dropped leaves a gap instead of shifting every later item to a
            # new slot (and a new fingerprint). Otherwise zero-padded
            # indices, so the slots sort the same lexically and numerically.
            width = max(1, len(str(len(items) - 1)))
            inherit = self._over_is_map_output(wf)
            slots = {
                (
                    Path(item.path).name if inherit else f"{index:0{width}d}"
                ): item
                for index, item in enumerate(items)
            }
            labels = {
                slot: Path(item.path).name for slot, item in slots.items()
            }
        root = Path(self.target.path_on_target(self.outputs[0]))
        await self.target.mkdir(str(root))

        # What each clone waits for before it runs, in order: its streamed
        # items, or a gap where a paired input lacks its slot.
        sources = wf.cached_source_map(None)[1]
        waits: dict[str, list[Callable[[], Awaitable[_Item]]]] = {
            slot: [] for slot in slots
        }
        paired: dict[str, dict[str, BaseArtifact]] = {
            slot: {} for slot in slots
        }
        for pair in (self.over, *self.zip):
            source = sources.get((self.id, pair.input_id))
            source_id = str(source.task_id if source else pair.input_id)
            pair_stream = streams.get(pair.input_id)
            if pair_stream is not None:
                for slot in slots:
                    # A slot the zip side never had is a gap, as below.
                    if pair is not self.over and slot not in pair_stream.slots:
                        waits[slot].append(partial(_gap, source_id))
                        continue
                    waits[slot].append(
                        partial(
                            pair_stream.wait,
                            slot,
                            owner=self.id,
                            source=source_id,
                        )
                    )
                    producer = pair_stream.clones.get(slot)
                    if pair is not self.over and producer is not None:
                        paired[slot][pair.item_id] = producer.outputs[0]
                continue
            if pair is self.over:
                continue
            # Paired by slot name. A slot the zip side lacks was dropped
            # there; one only it has was dropped on the over side.
            found = {
                Path(item.path).name: item
                for item in await self._iterable_input(pair.input_id).items(
                    self.target
                )
            }
            for slot in slots:
                if slot in found:
                    paired[slot][pair.item_id] = found[slot]
                else:
                    waits[slot].append(partial(_gap, source_id))

        clones: list[BaseTask] = []
        for slot, item in slots.items():
            slot_root = root / slot
            await self.target.mkdir(str(slot_root))
            clones.append(self._clone(slot, item, slot_root, paired[slot]))
        # Who writes each clone input, for the fast skip check: clones have
        # no edges, and an item of another map is that map's clone's output.
        maps = {t.id for t in wf.tasks if isinstance(t, MapTask)}
        writers = {
            artifact.id: (
                source.task_id
                if (source := sources.get((self.id, artifact.id)))
                else None
            )
            for artifact in self.inputs
        }
        for slot, clone in zip(slots, clones, strict=True):
            producers = dict(writers)
            for pair in (self.over, *self.zip):
                writer = writers.get(pair.input_id)
                producers[pair.item_id] = (
                    f"{writer}[{slot}]" if writer in maps else writer
                )
            clone._input_producers = producers  # noqa: SLF001
        # Clone ids repeat across a map retry: a stale record must not read
        # as this run's output. A slot a consumer took keeps it, so that
        # consumer finds the slot lost on a new machine instead of reading
        # it half rewritten.
        for slot, clone in zip(slots, clones, strict=True):
            if slot not in stream.taken:
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
        stream.open(labels, dict(zip(slots, clones, strict=True)), self.target)
        if streams:
            self._emit_chained(streams, labels)

        # Every clone input is already materialized on `self.target` (either
        # the item, or a copy of one of this task's own already-transferred
        # inputs), so it doubles as its own transfer origin: a co-located
        # clone target no-ops through BaseTransferStrategy.transfer's
        # same-location_id shortcut, a non-co-located one gets a real
        # transfer via the registered strategy. Nothing needs `pinned`.
        # These hand-offs are not workflow edges, so they emit no
        # TransferEvents (one per clone x input would flood the bus).
        # A streamed item is not here: it comes from its producing clone,
        # a no-op on the same machine too, and reported when it crosses.
        # Every item is named after its slot, so an item from another
        # machine lands in a folder of its source's own.
        per_item: dict[tuple[str, str], EdgeSource] = {}
        for pair in (self.over, *self.zip):
            pair_stream = streams.get(pair.input_id)
            if pair_stream is None:
                continue
            crosses = (
                pair_stream.target is not None
                and pair_stream.target.location_id != self.target.location_id
            )
            source = sources[(self.id, pair.input_id)]
            into = (
                f"{self.target.resolved_working_directory}"
                f"/.horus_items/{source.task_id}"
                if crosses
                else None
            )
            for slot, clone in zip(slots, clones, strict=True):
                producer = pair_stream.clones.get(slot)
                if producer is not None:
                    per_item[(clone.id, pair.item_id)] = EdgeSource(
                        pair_stream.target,
                        producer.outputs[0],
                        task_id=producer.id,
                        report=crosses,
                        into=into,
                    )
        source_map: dict[tuple[str, str], EdgeSource] = {
            (clone.id, artifact.id): per_item.get((clone.id, artifact.id))
            or EdgeSource(
                self.target,
                artifact,
                report=False,
                digest=self._input_digests.get(artifact.id),
            )
            for clone in clones
            for artifact in clone.inputs
        }

        if streams:
            # Keeps the sources' machines until this map settles: its
            # clones still read from them.
            wf.leases.repull(self)
            self._holding = True

        # The clones are this task's real work, and they bypass its executor,
        # which is what flips a task from CONFIGURING to RUNNING; without
        # this the map reports "configuring" for its whole fan-out.
        self.status = TaskStatus.RUNNING

        # A replaced machine comes back empty: stage it again before the
        # pending clones run on it.
        self.target.on_ready(self._restage)

        modes = self._consumer_modes(wf)
        run_clone = partial(
            self._run_item,
            stream,
            waits,
            modes,
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
            ):
                # Clones are independent: one failing must not cancel the
                # rest.
                results = list(
                    await asyncio.gather(
                        *map(run_clone, clones), return_exceptions=True
                    )
                )
            else:
                # A drop returns its error, so only a fatal one cancels
                # the rest.
                async with asyncio.TaskGroup() as tg:
                    runs = [tg.create_task(run_clone(c)) for c in clones]
                results = [run.result() for run in runs]
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
        if streams:
            # A source's folder is final once it closed (drops removed, lost
            # slots run again); shielded, so a cancel leaves them alone.
            closed = await asyncio.gather(
                *(asyncio.shield(s.closed) for s in streams.values())
            )
            if not all(closed):
                # shortcut: parked until the scheduler cancels this map (its
                # source failed); revisit if a source can fail without raising.
                await asyncio.get_running_loop().create_future()
        await self._settle(wf, root, labels, clones, results)
        # Ran only if a clone did: one that skipped rewrote nothing.
        if any(clone.runs for clone in clones):
            wf.mark_ran(self)
        # Before any close: a chained consumer then finds the root seeded.
        await self._seed_folder_digest(wf, clones, modes)
        # This map's folder is final: its consumers need not wait for the
        # digests below, unless a lost machine there can retry this map.
        if not (
            streams
            and self._retry_on_target_loss
            and any(
                t is not None and t.releasable
                for t in (self.target, *(s.target for s in streams.values()))
            )
        ):
            stream.close(ok=True)
        if streams:
            # Fingerprinted again, now that they settled.
            self._streaming_from = frozenset()
            self._streamed_digests = await wf.source_digests(
                self, wf.cached_source_map(None)[1], only=streams.keys()
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

    async def _run_item(
        self,
        stream: _ItemStream,
        waits: Mapping[str, list[Callable[[], Awaitable[_Item]]]],
        modes: set[CacheMode],
        run_task: Callable[[BaseTask], Awaitable[None]],
        clone: BaseTask,
    ) -> BaseException | None:
        """
        Run *clone* once each item it *waits* for is ready, digest its slot
        in the consumers' cache *modes*, and publish how it settled on
        *stream*. The wait comes first, so a waiting clone holds no slot,
        placement or lease. An item dropped upstream skips the clone. When it
        exits with one of :attr:`drop_exit_codes`, mark it dropped. A
        droppable failure is returned, not raised, so it does not cancel the
        siblings; :meth:`_settle` drops its slot.
        """
        slot = clone.id[len(self.id) + 1 : -1]
        try:
            for wait in waits.get(slot, []):
                item = await wait()
                if item.state == "dropped":
                    self._mark_upstream_dropped(clone, slot, str(item.origin))
                    stream.dropped(slot, str(item.origin), item.reason)
                    return None
                if item.state == "failed":
                    stream.failed()
                    # shortcut: parked until the scheduler cancels this map
                    # (its source is failing); revisit if a source can fail
                    # without raising.
                    await asyncio.get_running_loop().create_future()
            await run_task(clone)
        except Exception as exc:
            code = self._drop_code(exc)
            if code is not None:
                # shortcut: set after the run raised, so a snapshot can show
                # FAILED for one push; mark it inside the run if that matters.
                clone.status = TaskStatus.SKIPPED
                clone.skip_reason = SkipReason.DROPPED
                clone.error = _("Dropped: exit code %(code)d") % {"code": code}
                stream.dropped(
                    slot, self.id, _("exit code %(code)d") % {"code": code}
                )
                return exc
            if self._droppable(exc):
                stream.dropped(
                    slot, self.id, (clone.error or "")[:_ERROR_CHARS]
                )
                return exc
            stream.failed()
            raise
        else:
            # Before ready: a chained clone reading the slot shares it.
            await self._digest_slot(clone, modes)
            stream.ready(slot)
            return None
        finally:
            # Cancelled, or never settled: its consumer must not wait on.
            stream.failed(slot)

    def _consumer_modes(self, wf: BaseWorkflow) -> set[CacheMode]:
        """The cache modes this map's output is read in by its consumers."""
        tasks = {task.id: task for task in wf.tasks}
        return {
            wf.cache_mode(tasks[edge.target])
            for edge in wf.edges
            if edge.source == self.id
            and edge.transfer
            and edge.target in tasks
        }

    async def _digest_slot(
        self, clone: BaseTask, modes: set[CacheMode]
    ) -> None:
        """
        Digest *clone*'s slot on its own machine, once per run and mode, for
        the folder digest and the slot's readers.
        """
        wf = self.workflow
        if wf is None:
            return
        for mode in sorted(modes):
            try:
                await wf.digest(clone.target, clone.outputs[0], mode)
            except Exception as exc:
                # Only a saving: a reader digests the slot itself.
                horus_logger.log.debug(
                    _("Could not digest the slot of %(task_name)s: %(error)s")
                    % {"task_name": clone.name, "error": exc}
                )

    async def _seed_folder_digest(
        self, wf: BaseWorkflow, clones: list[BaseTask], modes: set[CacheMode]
    ) -> None:
        """
        Record this map's folder digest in each of *modes*, composed from its
        slots' digests instead of reading the folder again: equal to a
        recompute, as a folder digest is its entries' digests composed.
        """
        if not modes:
            return
        output = self.outputs[0]
        by_slot = {clone.id[len(self.id) + 1 : -1]: clone for clone in clones}
        try:
            entries = await self.target.list_dir(
                self.target.path_on_target(output)
            )
        except Exception:
            return  # only a saving: its readers digest the folder themselves
        listed = [entry.name for entry in entries]
        # Anything but a slot in the folder: its readers digest it whole.
        if not listed or any(name not in by_slot for name in listed):
            return
        for mode in sorted(modes):
            children: dict[str | bytes, str] = {}
            for name in listed:
                clone = by_slot[name]
                try:
                    digest = await wf.digest(
                        clone.target, clone.outputs[0], mode
                    )
                except Exception:
                    digest = None  # its readers digest the folder themselves
                if digest is None:
                    break
                children[name] = digest
            else:
                wf.seed_digest(
                    self.target,
                    output,
                    mode,
                    compose_folder_digest(mode, children),
                )

    def _mark_upstream_dropped(
        self, clone: BaseTask, slot: str, origin: str
    ) -> None:
        """*clone* never runs: its item was dropped in the map *origin*."""
        clone.status = TaskStatus.SKIPPED
        clone.skip_reason = SkipReason.UPSTREAM_DROPPED
        clone.finished_at = datetime.now(UTC)
        self._upstream_dropped[slot] = origin

    def _emit_chained(
        self, streams: dict[str, _ItemStream], labels: dict[str, str]
    ) -> None:
        """Say that this map starts each item as its sources finish it."""
        memo = self._chain_memo
        chain = sorted(
            {
                memo[1][input_id].id
                for input_id in streams
                if memo is not None and input_id in memo[1]
            }
        )
        HorusContext.get_context().bus.emit(
            HorusTaskEvent(
                task_id=self.id,
                task_name=self.name,
                message=_(
                    "Map %(name)s starts each item when %(sources)s "
                    "finishes it."
                )
                % {"name": self.name, "sources": ", ".join(chain)},
                data={
                    "map": {
                        "chain": chain,
                        "slots": dict(list(labels.items())[:_EVENT_ITEMS]),
                        "slot_count": len(labels),
                    }
                },
            )
        )

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
        # A configuration error is never the item's fault.
        if isinstance(exc, (asyncio.CancelledError, MapConfigurationError)):
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
                # A streaming map still reads its sources' machines.
                if not self._holding:
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
                # A dropped slot has no output to lose.
                and clone.skip_reason
                not in (SkipReason.DROPPED, SkipReason.UPSTREAM_DROPPED)
                and wf.leases.output_lost(
                    clone.id, clone.outputs[0].id, self.target
                )
                and self._take_rerun(clone)
            ):
                self._early_reruns[index] = asyncio.create_task(
                    run_clone(clone), context=context.copy()
                )

    def _take_rerun(self, clone: BaseTask) -> bool:
        """
        Spend *clone*'s one re-run of a lost slot and say so; ``False`` once
        it is spent. The clone leaves a terminal status, so the event says
        why. A consumer that has not asked for the slot yet waits for it.
        """
        if clone._target_loss_retries >= MAX_TARGET_LOSS_RETRIES:  # noqa: SLF001
            return False
        clone._target_loss_retries += 1  # noqa: SLF001
        if self._stream is not None:
            self._stream.again(clone.id[len(self.id) + 1 : -1])
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
        run_clone: Callable[[BaseTask], Awaitable[BaseException | None]],
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
                # A dropped slot has no output to lose.
                and clone.skip_reason
                not in (SkipReason.DROPPED, SkipReason.UPSTREAM_DROPPED)
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
        wf: BaseWorkflow,
        root: Path,
        labels: dict[str, str],
        clones: list[BaseTask],
        results: list[BaseException | None],
    ) -> None:
        """
        Turn the finished clones' *results* into the map's outcome: raise
        if a clone failed and is not droppable, or if every item was
        dropped, here or upstream. Then remove each dropped slot, so
        nothing downstream sees it, and report the drop.
        """
        errors = [r for r in results if isinstance(r, BaseException)]
        # A cancelled clone is a cancel, not a failure: never group it.
        for error in errors:
            if isinstance(error, asyncio.CancelledError):
                raise error
        upstream = self._upstream_dropped
        if not errors and not upstream:
            return
        fatal = [error for error in errors if not self._droppable(error)]
        if fatal:
            raise BaseExceptionGroup(
                _("%(n)d of %(total)d map clone(s) failed.")
                % {"n": len(fatal), "total": len(clones)},
                fatal,
            )
        if len(errors) + len(upstream) == len(clones):
            # The clone errors ride along as the cause, for the traceback.
            cause = (
                BaseExceptionGroup(
                    _("%(n)d of %(total)d map clone(s) failed.")
                    % {"n": len(errors), "total": len(clones)},
                    errors,
                )
                if errors
                else None
            )
            raise TaskExecutionError(
                _("All %(n)d items were dropped in %(name)s.")
                % {"n": len(clones), "name": self.name}
            ) from cause

        lines: list[str] = []
        dropped: list[dict[str, str]] = []
        for (slot, label), clone, result in zip(
            labels.items(), clones, results, strict=True
        ):
            if result is None and slot not in upstream:
                continue
            self._dropped_slots.append(slot)
            if result is None:
                continue
            summary = (clone.error or "")[:_ERROR_CHARS]
            code = self._drop_code(result)
            dropped.append(
                {
                    "slot": slot,
                    "item": label,
                    "reason": summary
                    if code is None
                    else _("exit code %(code)d") % {"code": code},
                }
            )
            if len(lines) < _DROPS_LISTED:
                lines.append(f"{slot} ({label}): {summary}")
        removed = [str(root / slot) for slot in self._dropped_slots]
        wf.forget_digests(self.target, removed)
        await _remove_slots(self.target, removed)
        # Again: a reader may have digested a slot or an ancestor meanwhile.
        wf.forget_digests(self.target, removed)
        if len(dropped) > _DROPS_LISTED:
            lines.append(
                _("+%(n)d more") % {"n": len(dropped) - _DROPS_LISTED}
            )
        parts = []
        if dropped:
            parts.append(
                _("Map %(name)s dropped %(n)d of %(total)d item(s):")
                % {"name": self.name, "n": len(dropped), "total": len(clones)}
            )
            parts.extend(lines)
        if upstream:
            parts.append(
                _("%(n)d item(s) dropped upstream.") % {"n": len(upstream)}
            )

        # The message and `data` are capped; the clones hold every drop.
        HorusContext.get_context().bus.emit(
            HorusTaskEvent(
                task_id=self.id,
                task_name=self.name,
                level="WARNING",
                message="\n".join(parts),
                data={
                    "map": {
                        "dropped": dropped[:_EVENT_ITEMS],
                        "dropped_count": len(dropped),
                        "upstream_dropped": dict(
                            sorted(upstream.items())[:_EVENT_ITEMS]
                        ),
                        "upstream_dropped_count": len(upstream),
                    }
                },
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
        streamed = self._streamed_digests
        if self._dropped_slots or (
            streamed is not None and None in streamed.values()
        ):
            # A streamed source that cannot be digested is a miss.
            await self._remove_manifest()
            return
        if streamed is not None:
            # Taken before the streamed inputs settled: take it again with
            # their final digests, as a run that did not stream records.
            fingerprint = await self._fingerprint(
                {**self._input_digests, **streamed}
            )
        await super()._write_manifest(fingerprint)

    async def is_complete(
        self, input_digests: InputDigests | None = None
    ) -> bool:
        """Never complete while streaming: the inputs have not settled."""
        if self.streamed_inputs:
            return False
        return await super().is_complete(input_digests)

    async def manifest_matches(
        self, manifest: bytes, input_digests: InputDigests
    ) -> bool:
        """Never a match while streaming: the inputs have not settled."""
        if self.streamed_inputs:
            return False
        return await super().manifest_matches(manifest, input_digests)

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
        self,
        slot: str,
        item: BaseArtifact,
        slot_root: Path,
        paired: Mapping[str, BaseArtifact],
    ) -> BaseTask:
        """
        One independent clone of this task for *slot*, bound to *item*, and
        to each *paired* item under its id, and rooted at *slot_root*.

        A clone is a plain :class:`~horus_builtin.task.horus_task.HorusTask`,
        so it runs the body instead of mapping over it again.
        """
        # This task's own inputs are already materialized on `self.target`
        # (its transfer step ran before `_execute`), but their
        # `declared_path` still holds whatever relative value they were
        # authored with. Freeze the current absolute path onto the copy.
        # A streamed folder is not there, and the body reads only its item.
        streamed = self.streamed_inputs
        inputs: list[BaseArtifact] = []
        for artifact in self.inputs:
            if artifact.id in streamed:
                continue
            copy = artifact.model_copy(deep=True)
            copy.declared_path = copy.path
            inputs.append(copy)

        # The item joins them as a new artifact under `over.as`, so the body
        # addresses this one element through `$<as>` while the collection it
        # came from stays addressable through `$<input_id>`.
        inputs.append(item.model_copy(update={"id": self.over.item_id}))
        inputs.extend(
            artifact.model_copy(update={"id": item_id})
            for item_id, artifact in paired.items()
        )

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
        # A zip folder likewise: its item fingerprints the clone.
        clone.unfingerprinted_inputs = [
            pair.input_id
            for pair in (self.over, *self.zip)
            if pair.input_id not in streamed
            and not self._body_may_read(pair.input_id)
        ]

        # The shared inputs are the copies this task just digested (a
        # digest does not depend on the path): the clone hashes its item
        # only. The scheduler replaces this when it digested them itself.
        clone._staged_digests = dict(self._input_digests)  # noqa: SLF001

        # Register this clone's item as a side artifact, so the upload
        # middleware persists it to S3 under the clone's own task id and the
        # UI can inspect exactly what this one run of the body received. The
        # item is an input transfer, not an output of the body, so without
        # this it would never be uploaded at all. A streamed item is the
        # producing clone's output, already uploaded under that clone.
        if self.over.input_id not in streamed:
            clone.side_artifacts.append(
                item.model_copy(update={"id": f"{clone.id}_item"})
            )

        return clone
