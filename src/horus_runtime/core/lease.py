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
from typing import TYPE_CHECKING, Any, ClassVar

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
"""Seconds between ``keepalive()`` calls on every live machine."""

EVACUATION_CONCURRENCY = 4
"""Outputs evacuated at once from one machine."""


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
    pinned: bool = False
    """Left alone until run end, after a failed drain or destroy."""


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

    def __init__(self, workflow: "BaseWorkflow") -> None:
        self.workflow = workflow
        self._leases: dict[str, Lease] = {}
        self._inputs_done: set[str] = set()
        self._produced: dict[tuple[str, str], int] = {}
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
        """The lease on *target*, created on first sight; ``None`` if none."""
        # `auto_teardown: false` is an explicit request to keep the machine.
        if not target.releasable or not getattr(target, "auto_teardown", True):
            return None
        lease = self._leases.get(target.lease_key)
        if lease is None:
            lease = Lease(key=target.lease_key, target=target)
            if target.max_parallel is not None:
                lease.slots = asyncio.Semaphore(target.max_parallel)
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

    # Per-task hooks (``execute_task``)

    async def member_slot(self, task: "BaseTask") -> None:
        """
        Wait for a ``max_parallel`` slot on *task*'s machine. A task that
        dispatches its own children (a map) takes none, or a cap of 1 would
        deadlock it against its clones.
        """
        lease = self._lease_for(task.target)
        if (
            lease is not None
            and lease.slots is not None
            and not task.dispatches_children
        ):
            await lease.slots.acquire()

    def member_started(self, task: "BaseTask") -> None:
        """*task* is bound to its machine and about to use it."""
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

    def member_finished(self, task: "BaseTask") -> None:
        """
        *task* stopped using its machine (any outcome). What happens next is
        decided by the scheduler's next :meth:`reconcile`.
        """
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
        source_map = self._source_map()
        for lease in self._leases.values():
            if lease.work is not None or lease.pinned or lease.running:
                continue
            if not lease.target.is_live:
                continue
            members = {
                task_id
                for task_id in pending
                if state.tasks[task_id].target.lease_key == lease.key
            }
            if members & set(state.in_flight):
                continue
            needed = self._needed(lease, pending, source_map)
            if not members:
                reason = "drained" if needed else "done"
                lease.work = asyncio.create_task(
                    self._release(lease, needed, reason)
                )
            elif lease.timer is None:
                lease.phase = Phase.HOLDING
                lease.target.emit_node_phase("holding")
                lease.timer = asyncio.create_task(self._hold(lease))

    def _source_map(self) -> "dict[tuple[str, str], EdgeSource]":
        self._source_map_cache = self.workflow.cached_source_map(
            self._source_map_cache
        )
        return self._source_map_cache[1]

    def _needed(
        self,
        lease: Lease,
        pending: set[str],
        source_map: "Mapping[tuple[str, str], EdgeSource]",
    ) -> set[tuple[str, str]]:
        """
        ``(producer, output)`` on *lease*'s machine that a pending task has
        not pulled yet, members included: they may re-provision elsewhere.
        """
        return {
            (str(src.task_id), src.artifact.id)
            for (consumer, _input), src in source_map.items()
            if consumer in pending
            and consumer not in self._inputs_done
            and src.target is not None
            and src.artifact is not None
            and src.task_id is not None
            and src.target.lease_key == lease.key
        }

    async def _hold(self, lease: Lease) -> None:
        """Release *lease* if it is still idle when the hold expires."""
        await asyncio.sleep(lease.target.hold_timeout_s or 0)
        lease.timer = None
        if lease.running or lease.work is not None or self._state is None:
            return
        needed = self._needed(lease, self._state.pending, self._source_map())
        lease.work = asyncio.create_task(
            self._release(lease, needed, "idle_cap")
        )

    async def _release(
        self, lease: Lease, needed: set[tuple[str, str]], reason: str
    ) -> None:
        """Evacuate *needed*, then destroy the machine. Never raises."""
        try:
            if needed:
                lease.phase = Phase.DRAINING
                lease.target.emit_node_phase("draining", outputs=len(needed))
                if not await self._evacuate(lease, needed):
                    # Never destroy data a pending task still needs: keep
                    # the machine and let consumers pull from it directly.
                    lease.target.emit_node_phase("drain_failed")
                    lease.pinned = True
                    return
            lease.phase = Phase.RELEASING
            await lease.target.release(reason)
        except Exception as exc:
            # The run-end teardown retries; this run must not fail for it.
            lease.pinned = True
            horus_logger.log.error(
                _("Releasing machine '%(key)s' failed: %(error)s")
                % {"key": lease.key, "error": exc}
            )
        finally:
            lease.work = None
            lease.phase = (
                Phase.BUSY
                if lease.running
                else Phase.HOLDING
                if lease.target.is_live
                else Phase.COLD
            )

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

        results = await asyncio.gather(
            *(one(*key) for key in sorted(needed)), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        for error in errors:
            horus_logger.log.error(
                _("Evacuating from machine '%(key)s' failed: %(error)s")
                % {"key": lease.key, "error": error}
            )
        return not errors

    async def _keep_alive(self) -> None:
        """Ping every live machine periodically (idle watchdog, life cap)."""
        while True:
            await asyncio.sleep(self.keepalive_interval_s)
            for lease in list(self._leases.values()):
                if lease.target.is_live and lease.phase not in (
                    Phase.RELEASING,
                    Phase.COLD,
                ):
                    await lease.target.keepalive()

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
            if lease.timer is not None:
                background.append(lease.timer)
                lease.timer = None
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
        self._produced.clear()
        self._state = None
        self.workflow.clear_relocations()
