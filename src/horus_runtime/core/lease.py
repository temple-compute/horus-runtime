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
Leases on releasable machines (e.g. terraform nodes), one manager per run.

A lease is held by the machine's *members* (pending tasks that run on it)
and by *consumers* (pending tasks elsewhere that still have to pull one of
its outputs). When no member runs while members are still pending, the
machine *holds* for ``hold_timeout_s``; once nothing holds it, or the hold
expires, every output a pending task still needs is evacuated through the
highest-priority :class:`BaseEvacuator` and the machine is released.

The scheduler drives it: :meth:`LeaseManager.reconcile` after every batch of
completions, plus the per-task hooks in ``execute_task``. Release lives here,
not in a task hook, because only the scheduler sees every way a task stops
being pending (completed, skipped, blocked, failed).
"""

import asyncio
from abc import abstractmethod
from collections.abc import Collection, Coroutine, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, cast

from horus_runtime.core.target.base import BaseTarget
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.registry.auto_registry import AutoRegistry

if TYPE_CHECKING:
    from horus_runtime.core.artifact.base import BaseArtifact
    from horus_runtime.core.placement import PlacementManager
    from horus_runtime.core.task.base import BaseTask
    from horus_runtime.core.workflow.base import BaseWorkflow, EdgeSource

KEEPALIVE_INTERVAL_S = 300.0
"""Seconds between ``keepalive()`` calls on every busy or holding machine."""

KEEPALIVE_TIMEOUT_S = 30.0
"""Cap on one ``keepalive()`` call."""

EVACUATION_TIMEOUT_S = 6 * 3600.0
"""Cap on one drain; past it the drain fails and the machine is kept."""

EVACUATION_CONCURRENCY = 4
"""Outputs evacuated at once from one machine."""

RELEASE_RETRY_S = 60.0
"""First wait before retrying a failed release; doubles up to an hour."""


class Phase(StrEnum):
    """Where a lease's machine is in its life."""

    COLD = "cold"
    """No machine (never provisioned, or released)."""
    BUSY = "busy"
    """Machine live, at least one member running."""
    HOLDING = "holding"
    """Machine live, members pending but none running; hold timer armed."""
    DRAINING = "draining"
    """Evacuating needed outputs before the release."""
    RELEASING = "releasing"
    """Destroying the machine."""


@dataclass(eq=False)
class Lease:
    """The lease on one declared releasable target (and all its copies)."""

    key: str
    target: BaseTarget
    running: set[str] = field(default_factory=set)
    timer: "asyncio.Task[None] | None" = None
    work: "asyncio.Task[None] | None" = None
    slots: asyncio.Semaphore | None = None
    phase: Phase = Phase.COLD
    retry: "asyncio.Task[None] | None" = None
    """Pending retry after a failed destroy; the lease is left alone."""
    failures: int = 0
    """Failed releases in a row, for the retry backoff."""
    no_cap: bool = False
    """After a failed drain: never released while anything still needs it."""


@dataclass(frozen=True)
class LeaseState:
    """The scheduler's view of the run, passed to :meth:`reconcile`."""

    tasks: Mapping[str, "BaseTask"]
    scope: Collection[str]
    completed: Collection[str]
    blocked: Collection[str]
    failed: Collection[str]
    in_flight: Collection[str] = ()
    """Dispatched and not finished, whether or not they started yet."""

    @property
    def pending(self) -> set[str]:
        """Tasks in scope that may still run."""
        return (
            set(self.scope)
            - set(self.completed)
            - set(self.blocked)
            - set(self.failed)
        )


class BaseEvacuator(AutoRegistry, entry_point="evacuator"):
    """
    Moves an output off a machine about to be released, returning where
    consumers read it from afterwards. The registered evacuator with the
    highest :attr:`priority` is used. Must not touch the machine after
    returning.
    """

    registry_key: ClassVar[str] = "kind"
    kind: str

    priority: ClassVar[int] = 0

    @abstractmethod
    async def evacuate(
        self, task: "BaseTask", output: "BaseArtifact", source: BaseTarget
    ) -> "EdgeSource":
        """Copy *output* of *task* off *source*; return its new source."""

    async def recover(
        self, task: "BaseTask", output: "BaseArtifact"
    ) -> "EdgeSource | None":
        """
        Where *output* of *task* can be read now that its machine is gone,
        without touching the machine; ``None`` if nowhere.
        """
        del task, output
        return None


class OrchestratorEvacuator(BaseEvacuator):
    """
    Copies the output into the orchestrator's working directory (under
    ``.evacuated/<task id>/``), with the strategy registered for the pair.
    """

    kind: str = "orchestrator"

    async def evacuate(
        self, task: "BaseTask", output: "BaseArtifact", source: BaseTarget
    ) -> "EdgeSource":
        """Copy *output* to the orchestrator."""
        # Local imports: both modules import this one.
        from horus_runtime.context import HorusContext  # noqa: PLC0415
        from horus_runtime.core.transfer.strategy import (  # noqa: PLC0415
            BaseTransferStrategy,
        )
        from horus_runtime.core.workflow.base import (  # noqa: PLC0415
            EdgeSource,
        )

        workflow = HorusContext.get_context().workflow
        orchestrator = workflow.orchestrator_target if workflow else None
        if orchestrator is None:
            raise RuntimeError(
                _("Cannot evacuate '%(out)s': no orchestrator target is set.")
                % {"out": output.id}
            )
        # Per task, so two outputs with the same file name do not collide.
        directory = (
            f"{orchestrator.resolved_working_directory}/.evacuated/{task.id}"
        )
        destination = orchestrator.model_copy(
            update={"working_directory": directory}
        )
        await destination.mkdir(directory)
        copy = output.model_copy()
        strategy_cls = BaseTransferStrategy.get_from_registry(
            source, destination
        )
        strategy = (
            strategy_cls()
            if strategy_cls is not None
            else source.fallback_transfer()
        )
        await strategy.transfer(copy, source, destination)
        return EdgeSource(destination, copy, task.id)


def _lease_keys(task: "BaseTask") -> set[str]:
    """
    Machines *task* will use: its own target's, plus those of the tasks of
    a subworkflow body it has not inlined yet.
    """
    keys = {task.target.lease_key}
    body = getattr(task, "body", None)
    for inner in getattr(body, "tasks", ()):
        keys |= _lease_keys(inner)
    return keys


def _pick_evacuator() -> BaseEvacuator:
    """The registered evacuator with the highest priority."""
    evacuator = max(
        BaseEvacuator.registry.values(), key=lambda cls: cls.priority
    )
    return evacuator.model_validate({})


class LeaseManager:
    """
    Decides when each releasable machine of one workflow run is drained and
    released. See the module docstring.

    Not thread-safe; single event loop, like the scheduler that drives it.
    """

    keepalive_interval_s: float = KEEPALIVE_INTERVAL_S
    release_retry_s: float = RELEASE_RETRY_S
    evacuation_timeout_s: float = EVACUATION_TIMEOUT_S

    def __init__(self, workflow: "BaseWorkflow") -> None:
        self.workflow = workflow
        self._leases: dict[str, Lease] = {}
        self._inputs_done: set[str] = set()
        self._pulling: set[str] = set()
        self._queued: set[str] = set()
        self._pulled = asyncio.Event()
        self._reading: dict[str, int] = {}
        self._snapshots: dict[tuple[str, str], bytes | None] = {}
        self._produced: dict[tuple[str, str], int] = {}
        self._recovering: dict[tuple[str, str], asyncio.Lock] = {}
        self._state: LeaseState | None = None
        self._keeper: asyncio.Task[None] | None = None
        self._source_map_cache: (
            tuple[int, dict[tuple[str, str], EdgeSource]] | None
        ) = None

    # Lease lookup

    @property
    def has_leases(self) -> bool:
        """Whether any releasable machine was seen in this run yet."""
        return bool(self._leases)

    def _lease_for(self, target: BaseTarget) -> Lease | None:
        """The lease on *target*, if it has one."""
        return self._leases.get(target.lease_key)

    def _open_lease(self, target: BaseTarget) -> Lease | None:
        """The lease on *target*, created on first sight; ``None`` if none."""
        if not target.releasable:
            return None
        lease = self._leases.get(target.lease_key)
        if lease is None:
            lease = Lease(key=target.lease_key, target=target)
            cap = target.member_cap
            if cap is not None:
                lease.slots = asyncio.Semaphore(cap)
            self._declare_capacity(target, self.workflow.placement)
            self._leases[lease.key] = lease
            if self._keeper is None:
                self._keeper = asyncio.create_task(self._keep_alive())
        return lease

    def _declare_capacity(
        self, target: BaseTarget, placement: "PlacementManager"
    ) -> None:
        """
        Gate members that declare ``resources`` on the machine's real size,
        unless the workflow already declared capacity for it.
        """
        capacity = target.node_capacity()
        if capacity is None or target.location_id in (
            self.workflow.capacity or {}
        ):
            return
        placement.declare(target.location_id, capacity)

    def produced_generation(self, task_id: str, output_id: str) -> int | None:
        """Machine generation *task_id* produced *output_id* on, if known."""
        return self._produced.get((task_id, output_id))

    def output_lost(
        self, task_id: str, output_id: str, target: BaseTarget
    ) -> bool:
        """
        Whether *output_id* of *task_id* went away with its machine: the
        machine is down, or not the generation that produced it.
        """
        produced = self._produced.get((task_id, output_id))
        return produced is not None and (
            not target.is_live or produced != target.generation
        )

    async def recover(self, source: "EdgeSource") -> "EdgeSource | None":
        """
        Where the lost output of *source* can be read now, recorded as a
        relocation; ``None`` if nowhere. One recovery per output at a time.
        """
        assert source.task_id is not None and source.artifact is not None
        key = (str(source.task_id), source.artifact.id)
        async with self._recovering.setdefault(key, asyncio.Lock()):
            # Another consumer may have recovered it while this one waited.
            relocated = self.workflow._relocated(source)  # noqa: SLF001
            if relocated is not source:
                return relocated
            task = next(
                (t for t in self.workflow.tasks if t.id == key[0]), None
            )
            if task is None:
                return None
            recovered = await _pick_evacuator().recover(task, source.artifact)
            if recovered is not None:
                self.workflow.relocate_output(*key, recovered)
            return recovered

    # Per-task hooks (``execute_task``)

    async def member_slot(self, task: "BaseTask") -> None:
        """
        Wait for a ``member_cap`` slot on *task*'s machine. A task that
        dispatches its own children (a map) takes none, or a cap of 1 would
        deadlock it against its clones.
        """
        lease = self._open_lease(task.target)
        if (
            lease is not None
            and lease.slots is not None
            and not task.dispatches_children
        ):
            await lease.slots.acquire()

    def member_started(self, task: "BaseTask") -> None:
        """*task* is bound to its machine and about to pull its inputs."""
        self._queued.discard(task.id)
        self._pulling.add(task.id)
        lease = self._lease_for(task.target)
        if lease is None:
            return
        lease.running.add(task.id)
        if lease.timer is not None:
            lease.timer.cancel()
            lease.timer = None
        if lease.phase is Phase.HOLDING:
            lease.target.emit_node_phase("busy")
        if lease.work is None:
            lease.phase = Phase.BUSY

    def inputs_transferred(self, task: "BaseTask") -> None:
        """*task* no longer needs any other machine's outputs."""
        self._inputs_done.add(task.id)
        self._done_pulling(task)

    def repull(self, task: "BaseTask") -> None:
        """*task* pulls its inputs again (a retry, or a map's restage)."""
        self._inputs_done.discard(task.id)
        self._pulling.add(task.id)

    def _done_pulling(self, task: "BaseTask") -> None:
        self._pulling.discard(task.id)
        self._pulled.set()

    def member_finished(self, task: "BaseTask") -> None:
        """
        *task* stopped using its machine (any outcome). What happens next is
        decided by the scheduler's next :meth:`reconcile`.
        """
        self._done_pulling(task)
        self._queued.discard(task.id)
        lease = self._lease_for(task.target)
        if lease is None:
            return
        if task.id in lease.running:
            lease.running.discard(task.id)
            for output in task.outputs:
                self._produced[(task.id, output.id)] = lease.target.generation
        if lease.slots is not None and not task.dispatches_children:
            lease.slots.release()

    # Decisions

    def reconcile(self, state: LeaseState) -> None:
        """
        Re-decide every lease from the scheduler's current view: hold,
        drain and release, or leave alone. Never blocks on a destroy; the
        work runs in the background.
        """
        self._state = state
        pending = state.pending
        starting = set(state.in_flight) - self._queued
        members_of: dict[str, set[str]] = {}
        for task_id in pending:
            for key in _lease_keys(state.tasks[task_id]):
                members_of.setdefault(key, set()).add(task_id)
        for lease in self._leases.values():
            if lease.work is not None or lease.retry or lease.running:
                continue
            if not lease.target.is_live:
                continue
            members = members_of.get(lease.key, set())
            if members & starting:
                # About to start (waiting on its own slot or placement).
                self._cancel_timer(lease)
                continue
            if members:
                # After a failed drain too: the expiry retries the drain.
                if lease.timer is None:
                    if lease.phase is not Phase.HOLDING:
                        lease.phase = Phase.HOLDING
                        lease.target.emit_node_phase("holding")
                    lease.timer = asyncio.create_task(self._hold(lease))
                continue
            needed = self._needed(lease, state, pending)
            sentinels = self._sentinels(lease, state, pending)
            if lease.no_cap and needed:
                # Consumers pull from the machine directly.
                continue
            self._cancel_timer(lease)
            reason = "drained" if needed or sentinels else "done"
            lease.work = asyncio.create_task(
                self._release(lease, needed, reason, sentinels)
            )

    def _source_map(self) -> "dict[tuple[str, str], EdgeSource]":
        self._source_map_cache = self.workflow.cached_source_map(
            self._source_map_cache
        )
        return self._source_map_cache[1]

    @staticmethod
    def _cancel_timer(lease: Lease) -> None:
        if lease.timer is not None:
            lease.timer.cancel()
            lease.timer = None

    @staticmethod
    def _members(lease: Lease, state: LeaseState) -> set[str]:
        """
        Pending tasks that will run on *lease*'s machine, counting a
        not-yet-expanded subworkflow whose body has a task there.
        """
        return {
            task_id
            for task_id in state.pending
            if lease.key in _lease_keys(state.tasks[task_id])
        }

    def _sentinels(
        self, lease: Lease, state: LeaseState, pending: set[str]
    ) -> set[tuple[str, str]]:
        """
        ``(producer, output)`` on *lease*'s machine that a pending task reads
        straight off the producer's target, outside any transfer: an edge
        condition's sentinel, or a loop body's signal.
        """
        sentinels: set[tuple[str, str]] = set()
        if lease.target.keeps_data_on_release:
            return sentinels
        for edge in self.workflow.edges:
            consumer = state.tasks.get(edge.target)
            if consumer is None or edge.target not in pending:
                continue
            condition = edge.condition
            if condition is not None:
                key = (
                    condition.source_task or edge.source,
                    condition.source_output or edge.source_output,
                )
            # A loop checker reads the previous body's signal output.
            elif edge.source_output is not None and edge.source_output == (
                getattr(consumer, "signal_output", None)
            ):
                key = (edge.source, edge.source_output)
            else:
                continue
            producer = state.tasks.get(key[0])
            if (
                key[1] is not None
                and producer is not None
                and producer.target.lease_key == lease.key
                and key not in self._snapshots
            ):
                sentinels.add(cast(tuple[str, str], key))
        return sentinels

    async def read_output(
        self, task: "BaseTask", artifact: "BaseArtifact"
    ) -> bytes | None:
        """
        Bytes of *task*'s *artifact* read off its target, or from the
        snapshot taken before that machine was released; ``None`` when it
        was never written.
        """
        key = (task.id, artifact.id)
        if key in self._snapshots:
            return self._snapshots[key]
        lease_key = task.target.lease_key
        # A release waits for reads in progress (see `_release`).
        self._reading[lease_key] = self._reading.get(lease_key, 0) + 1
        try:
            path = task.target.path_on_target(artifact)
            if not await task.target.path_exists(path):
                return None
            return await task.target.get_file(path)
        finally:
            self._reading[lease_key] -= 1
            self._pulled.set()

    def _needed(
        self, lease: Lease, state: LeaseState, pending: set[str]
    ) -> set[tuple[str, str]]:
        """
        ``(producer, output)`` on *lease*'s machine that a pending task has
        not pulled yet, members included: they may re-provision elsewhere.
        A pending subworkflow needs whatever feeds it, data edge or not: its
        body re-emits those edges onto real consumers when it expands.
        """
        if lease.target.keeps_data_on_release:
            return set()
        needed = {
            (str(src.task_id), src.artifact.id)
            for (consumer, _input), src in self._source_map().items()
            if consumer in pending
            and consumer not in self._inputs_done
            and src.target is not None
            and src.artifact is not None
            and src.task_id is not None
            and src.target.lease_key == lease.key
        }
        relocated = self.workflow._relocations  # noqa: SLF001
        for edge in self.workflow.edges:
            consumer = state.tasks.get(edge.target)
            producer = state.tasks.get(edge.source)
            if (
                consumer is not None
                and producer is not None
                and edge.source_output is not None
                and edge.target in pending
                and getattr(consumer, "body", None) is not None
                and producer.target.lease_key == lease.key
                and (edge.source, edge.source_output) not in relocated
            ):
                needed.add((edge.source, edge.source_output))
        return needed

    def _pulling_from(self, lease: Lease) -> bool:
        """
        Whether a task is still pulling inputs produced on *lease*'s machine
        (it may have resolved its source before a relocation).
        """
        tasks = {task.id: task for task in self.workflow.tasks}
        return any(
            edge.target in self._pulling
            and edge.source in tasks
            and tasks[edge.source].target.lease_key == lease.key
            for edge in self.workflow.edges
        )

    def member_queued(self, task: "BaseTask") -> None:
        """
        *task* waits for a ``max_concurrency`` slot: until it gets one it
        does not keep its machine from holding (see :meth:`reconcile`).
        """
        self._queued.add(task.id)
        lease = self._lease_for(task.target)
        # A running member, hold, drain or retry already decides the lease:
        # skip the full reconcile, which a map fan-out would run per clone.
        if (
            self._state is not None
            and lease is not None
            and not (lease.running or lease.timer or lease.work or lease.retry)
        ):
            self.reconcile(self._state)

    async def _hold(self, lease: Lease) -> None:
        """Release *lease* if it is still idle when the hold expires."""
        await asyncio.sleep(lease.target.hold_timeout_s or 0)
        lease.timer = None
        state = self._state
        if (
            lease.running
            or lease.work is not None
            or state is None
            or self._members(lease, state)
            & (set(state.in_flight) - self._queued)
        ):
            return
        pending = state.pending
        lease.work = asyncio.create_task(
            self._release(
                lease,
                self._needed(lease, state, pending),
                "idle_cap",
                self._sentinels(lease, state, pending),
            )
        )

    async def _release(
        self,
        lease: Lease,
        needed: set[tuple[str, str]],
        reason: str,
        sentinels: set[tuple[str, str]] = frozenset(),  # type: ignore[assignment]
    ) -> None:
        """
        Snapshot *sentinels*, evacuate *needed*, then destroy the machine.
        Never raises.
        """
        try:
            if needed or sentinels:
                lease.phase = Phase.DRAINING
                lease.target.emit_node_phase("draining", outputs=len(needed))
                if not await self._snapshot(
                    lease, sentinels
                ) or not await self._evacuate(lease, needed):
                    # Never destroy data a pending task still needs: keep
                    # the machine and let consumers pull from it directly.
                    lease.target.emit_node_phase("drain_failed")
                    lease.no_cap = True
                    return
            # A transfer or read that started before the relocation still
            # reads from this machine: let it finish.
            while self._pulling_from(lease) or self._reading.get(lease.key):
                self._pulled.clear()
                await self._pulled.wait()
            if lease.running:
                # A member started meanwhile: what it needs is relocated
                # already, and it is using the machine. Keep it.
                return
            lease.phase = Phase.RELEASING
            await lease.target.release(reason)
            lease.failures = 0
            lease.no_cap = False
        except Exception as exc:
            # Retried with backoff; this run must not fail for it.
            lease.retry = asyncio.create_task(self._retry_release(lease))
            horus_logger.log.error(
                _("Releasing machine '%(key)s' failed: %(error)s")
                % {"key": lease.key, "error": exc}
            )
        finally:
            lease.work = None
            phase = (
                Phase.BUSY
                if lease.running
                else Phase.HOLDING
                if lease.target.is_live
                else Phase.COLD
            )
            # Back to busy after a drain, or kept after a failed destroy.
            if (
                phase is Phase.BUSY
                and lease.phase in (Phase.DRAINING, Phase.RELEASING)
            ) or (phase is Phase.HOLDING and lease.phase is Phase.RELEASING):
                lease.target.emit_node_phase(phase.value)
            lease.phase = phase

    async def _retry_release(self, lease: Lease) -> None:
        """Re-decide *lease* after a backoff that doubles per failure."""
        delay = min(self.release_retry_s * 2**lease.failures, 3600.0)
        lease.failures += 1
        await asyncio.sleep(delay)
        lease.retry = None
        if self._state is not None:
            self.reconcile(self._state)

    async def _snapshot(
        self, lease: Lease, sentinels: set[tuple[str, str]]
    ) -> bool:
        """Keep the bytes of every output in *sentinels*; whether it worked."""
        tasks = {task.id: task for task in self.workflow.tasks}
        try:
            for task_id, output_id in sorted(sentinels):
                task = tasks[task_id]
                output = next(o for o in task.outputs if o.id == output_id)
                self._snapshots[(task_id, output_id)] = await self.read_output(
                    task, output
                )
        except Exception as exc:
            horus_logger.log.error(
                _("Evacuating from machine '%(key)s' failed: %(error)s")
                % {"key": lease.key, "error": exc}
            )
            return False
        return True

    async def _evacuate(
        self, lease: Lease, needed: set[tuple[str, str]]
    ) -> bool:
        """Evacuate every output in *needed*; whether all of them made it."""
        evacuator = _pick_evacuator()
        tasks = {task.id: task for task in self.workflow.tasks}
        semaphore = asyncio.Semaphore(EVACUATION_CONCURRENCY)

        async def one(task_id: str, output_id: str) -> None:
            async with semaphore:
                task = tasks[task_id]
                output = next(o for o in task.outputs if o.id == output_id)
                source = await evacuator.evacuate(task, output, lease.target)
                self.workflow.relocate_output(task_id, output_id, source)

        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(one(*key) for key in sorted(needed)),
                    return_exceptions=True,
                ),
                self.evacuation_timeout_s,
            )
        except TimeoutError as exc:
            results = [exc]
        errors = [r for r in results if isinstance(r, BaseException)]
        for error in errors:
            horus_logger.log.error(
                _("Evacuating from machine '%(key)s' failed: %(error)s")
                % {"key": lease.key, "error": error}
            )
        return not errors

    async def _keep_alive(self) -> None:
        """
        Ping every live machine that a member is running on, or that is in
        its hold, so its idle watchdog and life cap do not fire. A machine
        stuck draining, or kept after a failed drain or destroy, is not
        pinged outside a hold.
        """
        while True:
            await asyncio.sleep(self.keepalive_interval_s)
            await asyncio.gather(
                *(
                    self._ping(lease)
                    for lease in list(self._leases.values())
                    if lease.target.is_live
                    and (lease.running or lease.timer is not None)
                )
            )

    async def _ping(self, lease: Lease) -> None:
        """One keepalive; a failed or hung ping must not stop the others."""
        try:
            await asyncio.wait_for(
                lease.target.keepalive(), KEEPALIVE_TIMEOUT_S
            )
        except Exception as exc:
            horus_logger.log.warning(
                _("Keepalive of machine '%(key)s' failed: %(e)s")
                % {"key": lease.key, "e": exc or type(exc).__name__}
            )

    # Run end

    async def aclose(self, *, abort: bool) -> None:
        """
        End of run: stop timers, finish (or, on *abort*, cancel) in-flight
        drains, then release every machine still live, without evacuation.
        """
        background: list[asyncio.Task[None]] = []
        if self._keeper is not None:
            background.append(self._keeper)
            self._keeper = None
        for lease in self._leases.values():
            for task in (lease.timer, lease.retry):
                if task is not None:
                    background.append(task)
            lease.timer = lease.retry = None
        if abort:
            background += [
                lease.work
                for lease in self._leases.values()
                if lease.work is not None
            ]
        for task in background:
            task.cancel()
        works = [
            lease.work
            for lease in self._leases.values()
            if lease.work is not None
        ]
        await asyncio.gather(*background, *works, return_exceptions=True)

        reason = "abort" if abort else "run_end"
        releases: list[Coroutine[Any, Any, None]] = [
            lease.target.release(reason)
            for lease in self._leases.values()
            if lease.target.is_live
        ]
        for result in await asyncio.gather(*releases, return_exceptions=True):
            if isinstance(result, BaseException):
                horus_logger.log.error(
                    _("Releasing a machine at run end failed: %(error)s")
                    % {"error": result}
                )

        # A workflow instance can run again: nothing carries over.
        self._leases.clear()
        self._inputs_done.clear()
        self._pulling.clear()
        self._queued.clear()
        self._reading.clear()
        self._snapshots.clear()
        self._pulled = asyncio.Event()
        self._produced.clear()
        self._state = None
        self.workflow.clear_relocations()
