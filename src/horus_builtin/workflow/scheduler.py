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
Concurrent ready-set scheduler for Horus built-in workflows.

Replaces a serial "run the topological order one task at a time" loop with a
scheduler that dispatches every currently-ready task concurrently and reacts
as each one finishes: as soon as a task completes, its dependents that are
now ready are dispatched too, without waiting for unrelated in-flight tasks.
"""

import asyncio
from typing import TYPE_CHECKING

from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.workflow.condition import compute_liveness
from horus_builtin.workflow.dag import (
    UnknownTaskError,
    ancestors,
    build_dependencies,
    descendants,
    topological_sort,
)
from horus_runtime.context import HorusContext
from horus_runtime.core.placement import PlacementManager
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.status import SkipReason, TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow, _EdgeSource
from horus_runtime.core.workflow.exceptions import WorkflowExecutionError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class TargetPool:
    """
    Hands out an idle target for each dispatched task, giving otherwise
    single-slot :class:`BaseTarget` instances room for concurrency.

    In a hand-authored DAG each task normally owns its own distinct target
    object, so there is no contention and every acquisition simply returns
    the task's declared target. The pool only has to do real work when two
    ready tasks share the exact same target *instance* (e.g. a workflow that
    reuses one ``LocalTarget()`` across several task placements): the second
    concurrent acquisition for that instance gets an idle clone
    (``target.model_copy()``) instead of blocking. A clone is a valid extra
    slot because targets sharing a target's class and fields also share its
    ``location_id`` (same filesystem), so no artifact transfer is needed
    between the original and the clone.

    Targets are keyed by object identity (``id(target)``), not by task id,
    because the sharing scenario above is about the target object, not the
    task.
    """

    def __init__(self, max_concurrency: int | None) -> None:
        # Idle instances available for a given declared target, keyed by
        # id(declared_target). Lazily seeded with the declared target itself
        # on first acquisition (see `acquire`).
        self._idle: dict[int, list[BaseTarget]] = {}
        self._semaphore = (
            asyncio.Semaphore(max_concurrency)
            if max_concurrency is not None
            else None
        )

    async def acquire(self, declared_target: BaseTarget) -> BaseTarget:
        """
        Return an idle target equivalent to *declared_target*.

        Blocks on the ``max_concurrency`` semaphore (if set) before handing
        out a target, so the cap applies to genuinely concurrent dispatches
        rather than just to distinct target instances.
        """
        if self._semaphore is not None:
            await self._semaphore.acquire()

        idle = self._idle.setdefault(id(declared_target), [declared_target])
        if idle:
            return idle.pop()

        # Every idle instance (the declared target and any prior clones) is
        # currently in use: mint another clone as an extra slot.
        clone = declared_target.model_copy()
        # model_copy() shallow-copies pydantic private attributes, so the
        # clone would otherwise start out pointing at the declared target's
        # in-flight `_task_future` and look "busy" before it has run
        # anything. Clear them so the clone starts genuinely idle.
        clone._task = None  # noqa: SLF001
        clone._task_future = None  # noqa: SLF001
        return clone

    def release(self, declared_target: BaseTarget, target: BaseTarget) -> None:
        """
        Return *target* (previously returned by :meth:`acquire` for
        *declared_target*) to the idle pool.
        """
        self._idle[id(declared_target)].append(target)
        if self._semaphore is not None:
            self._semaphore.release()


async def _execute_ready_task(
    workflow: BaseWorkflow,
    task: "BaseTask",
    source_map: dict[tuple[str, str], _EdgeSource],
    pool: TargetPool,
    placement: PlacementManager,
    liveness: dict[str, bool],
) -> None:
    """
    Run one ready task to completion: reserve placement, acquire a target,
    bind, transfer inputs, dispatch, and wait — mirroring the per-task body
    of the previous serial loop, but on whichever target the pool hands back
    (the task's own declared target in the common case, or an idle clone
    under contention).

    ``placement.acquire`` waits until the task's declared target's location
    has room for ``task.resources`` (immediately, when the task or location
    isn't resource-gated at all — see :class:`PlacementManager`), so a
    resource-constrained fan-out can genuinely hold this coroutine here
    without ever occupying a pool slot.

    A task on a branch that was not taken is skipped here and returns cleanly,
    so the caller counts it as completed and the DAG moves on (exactly as a
    memoized ``skip_if_complete`` task does). The check has to happen *before*
    ``placement.acquire`` and the transfer below: an inactive task's inputs
    were never produced, so transferring them would fail on any target where
    transfer is not a no-op.
    """
    if not await compute_liveness(workflow, task.id, liveness):
        task.status = TaskStatus.SKIPPED
        task.skip_reason = SkipReason.INACTIVE
        message = _(
            "Task %(task_name)s skipped: no incoming branch was taken."
        ) % {"task_name": task.name}
        horus_logger.log.debug(message)
        HorusContext.get_context().bus.emit(
            HorusTaskEvent(
                task_id=task.id,
                task_name=task.name,
                message=message,
            )
        )
        return

    declared_target = task.target
    location_id = declared_target.location_id
    await placement.acquire(task.name, location_id, task.resources)
    try:
        target = await pool.acquire(declared_target)
        # `transfer_artifacts` and `dispatch` both operate off `task.target`,
        # so point it at the target we actually acquired for the duration of
        # the run and restore it afterwards, leaving the task's declared
        # target untouched for any subsequent run of the same workflow
        # instance.
        task.target = target
        try:
            # Associate the task with its target before any transfer so
            # resource-aware targets (which may provision lazily at transfer
            # time, before dispatch) can read task.resources.
            target.bind(task)

            # Transfer input artifacts to the task's target as needed.
            await workflow.transfer_artifacts(task, source_map)

            # Execute the task on its target and wait for it to finish.
            try:
                await target.dispatch(task)
                await target.wait()
            except asyncio.CancelledError:
                # Cancelling this wrapper also cancels the inner task future
                # (cancellation propagates through `target.wait()`'s await),
                # so by now the task has recorded CANCELED and the future is
                # done; `target.cancel()` would early-return. Call the
                # executor's kill hook directly so any external process
                # (container, remote job) that does not die with the
                # coroutine is terminated. Shielded so a second cancel
                # cannot abort the kill mid-flight.
                if task.status is TaskStatus.CANCELED:
                    await asyncio.shield(task.executor.cancel_execution())
                raise
        finally:
            task.target = declared_target
            pool.release(declared_target, target)
    finally:
        await placement.release(location_id, task.resources)


def _collect_completions(
    done: set[asyncio.Task[None]],
    running: dict[asyncio.Task[None], str],
    completed: set[str],
) -> list[tuple[str, BaseException]]:
    """
    Pop every finished task out of *running*, adding successes to
    *completed*, and return every ``(task_id, exception)`` failure
    encountered, in deterministic (task id) order.

    Under ``fail_fast`` only the first entry is ever used (the caller cancels
    and re-raises as soon as the list is non-empty), but ``continue`` needs
    every failure from the batch: several ready tasks can finish in the same
    ``asyncio.wait`` and more than one can fail at once.
    """
    failures: list[tuple[str, BaseException]] = []
    for fut in sorted(done, key=lambda f: running[f]):
        task_id = running.pop(fut)
        if fut.cancelled():
            continue
        exc = fut.exception()
        if exc is not None:
            failures.append((task_id, exc))
        else:
            completed.add(task_id)
    return failures


async def _cancel_running(running: dict[asyncio.Task[None], str]) -> None:
    """Cancel every still-running wrapper task and wait for it to unwind."""
    for fut in running:
        fut.cancel()
    if running:
        await asyncio.wait(list(running))
    running.clear()


def _dependencies_with_implicit(
    workflow: BaseWorkflow,
) -> dict[str, set[str]]:
    """
    Edge-derived dependency map for *workflow*, augmented with its runtime-only
    implicit deps (a task added mid-run gated behind its creator; see
    ``BaseWorkflow.add_task``/``expand``). Recomputed every scheduler loop so a
    mid-run mutation is reflected immediately, and merged before scope is
    derived so a newly added, edge-less task enters scope as a descendant of
    its in-scope creator.
    """
    deps = build_dependencies(workflow.tasks, workflow.edges)
    for task_id, creators in workflow.implicit_task_dependencies.items():
        if task_id in deps:
            deps[task_id] |= creators & deps.keys()
    return deps


async def run_schedule(workflow: BaseWorkflow, trigger_id: str) -> None:
    """
    Execute *workflow* from *trigger_id* with a concurrent ready-set
    scheduler.

    A task is skipped when all of its output artifacts exist (see
    ``BaseTask.is_complete``, applied inside ``BaseTask.run``). Every task
    whose dependencies are already satisfied is dispatched immediately and
    concurrently with any other ready task, bounded by
    ``workflow.max_concurrency`` when set.

    ``workflow.failure_policy`` controls what happens once a task fails:

    - ``"fail_fast"`` (the default): the first task to fail cancels every
      other task still in flight and re-raises immediately, so
      ``BaseWorkflow.run`` can mark the workflow ``FAILED`` — the same
      fail-fast contract the previous serial loop provided.
    - ``"continue"``: a failed task does not cancel anything. Its
      descendants are added to a ``blocked`` set and never dispatched (a
      failed or blocked dependency never enters ``completed``, so a
      dependent's ``deps[task_id] <= completed`` check never passes for it
      either — the ``blocked`` set just makes that explicit and avoids
      mistaking the resulting deadlock for a genuine cycle). Every other
      branch keeps running to completion. Once nothing more can run, if any
      task failed, :exc:`WorkflowExecutionError` is raised naming every
      failed task, so the workflow still ends ``FAILED``.

    Raises:
        UnknownTaskError: If trigger_id is not a task in the workflow.
        CyclicDependencyError: If the tasks in scope cannot all be
            scheduled (a cycle keeps the remainder permanently blocked).
        WorkflowExecutionError: Under the ``"continue"`` policy, if any task
            failed during the run.
    """
    if trigger_id not in {task.id for task in workflow.tasks}:
        raise UnknownTaskError(
            _("Trigger task '%(trigger_id)s' not found.")
            % {"trigger_id": trigger_id}
        )

    # No edges means no dependencies: every task runs independently and the
    # plan is limited to the trigger's own (singleton) scope. Flag it so a
    # workflow that forgot to wire its edges is diagnosable.
    if len(workflow.tasks) > 1 and not workflow.edges:
        horus_logger.log.debug(
            _(
                "Workflow %(name)s has multiple tasks but no edges; "
                "tasks run independently with no ordering."
            )
            % {"name": workflow.name}
        )

    pool = TargetPool(workflow.max_concurrency)
    placement = PlacementManager(workflow.capacity)

    # The source map depends only on workflow structure. It is rebuilt only
    # when the workflow's revision advances (nothing bumps it yet, so this
    # effectively builds once, the first time it's needed).
    source_map_cache: tuple[int, dict[tuple[str, str], _EdgeSource]] | None = (
        None
    )

    completed: set[str] = set()
    dispatched: set[str] = set()
    running: dict[asyncio.Task[None], str] = {}

    # `continue`-policy bookkeeping. Both stay empty for `fail_fast`, which
    # always raises out of the loop on the first failure instead of
    # populating them.
    blocked: set[str] = set()
    failed: dict[str, BaseException] = {}

    # Conditional-branch bookkeeping: task_id -> whether any incoming edge was
    # live. Populated by the gate in `_execute_ready_task` as each task is
    # dispatched, and read back there when deciding a task's own liveness, so
    # deactivation propagates down a chain without a second traversal. Distinct
    # from `blocked`: an inactive task still counts as completed, so a join
    # downstream of both branches of a fork still runs.
    liveness: dict[str, bool] = {}

    while True:
        # Recomputed every iteration (instead of once up front) so the
        # runtime DAG-mutation API (BaseWorkflow.add_task/add_edge/expand)
        # can grow `workflow.tasks`/`workflow.edges` mid-run without any
        # further change to this loop: `tasks_by_id` must be rebuilt here
        # too, otherwise a task added after the loop started would compute
        # as "ready" below but be missing from a stale lookup.
        tasks_by_id = {task.id: task for task in workflow.tasks}
        deps = _dependencies_with_implicit(workflow)
        scope = ancestors(trigger_id, deps) | descendants(trigger_id, deps)

        # Deterministic order when several tasks become ready at once,
        # matching topological_sort's tie-breaking. `blocked` tasks are
        # excluded explicitly for clarity: a blocked task's dependency never
        # entered `completed` either, so `deps[task_id] <= completed` alone
        # would already keep it (and everything downstream of it) out of
        # `ready`.
        ready = sorted(
            task_id
            for task_id in scope
            if task_id not in dispatched
            and task_id not in blocked
            and deps[task_id] <= completed
        )

        # Tasks left neither completed, blocked, nor failed, with nothing
        # ready or in flight to get them there, are stuck on each other
        # rather than on an (already accounted for) upstream failure.
        stuck = scope - completed - blocked - failed.keys()
        if not ready and not running and stuck:
            # Reuse topological_sort's cycle detection for a consistent
            # error instead of silently stopping short.
            topological_sort(scope, deps)

        if not ready and not running:
            # Nothing left that can ever become ready: either everything in
            # scope completed, or (continue policy) the rest is permanently
            # blocked behind a failure. Report any failures below the loop.
            break

        if ready:
            source_map_cache = workflow.cached_source_map(source_map_cache)
            source_map = source_map_cache[1]
            for task_id in ready:
                task = tasks_by_id[task_id]
                dispatched.add(task_id)
                wrapper = asyncio.create_task(
                    _execute_ready_task(
                        workflow, task, source_map, pool, placement, liveness
                    )
                )
                running[wrapper] = task_id

        try:
            done, _pending = await asyncio.wait(
                running, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # Workflow-level cancellation: `asyncio.wait` does not cancel the
            # tasks it waits on, so propagate the cancel to every in-flight
            # wrapper (each one kills its executor via `target.cancel()`, see
            # `_execute_ready_task`) and wait for them to unwind before
            # re-raising.
            await _cancel_running(running)
            raise

        failures = _collect_completions(done, running, completed)
        if failures and workflow.failure_policy == "fail_fast":
            await _cancel_running(running)
            raise failures[0][1]

        for task_id, exc in failures:
            # `continue` policy: record the failure and block its
            # descendants (inclusive of task_id itself) instead of aborting.
            # Unrelated branches already in `running`, or that become ready
            # on a later iteration, are left alone.
            failed[task_id] = exc
            newly_blocked = descendants(task_id, deps) - blocked
            blocked |= newly_blocked
            for blocked_id in sorted(newly_blocked - {task_id}):
                blocked_task = tasks_by_id[blocked_id]
                message = _(
                    "Task %(task_name)s blocked: upstream task "
                    "%(failed_task_name)s failed."
                ) % {
                    "task_name": blocked_task.name,
                    "failed_task_name": tasks_by_id[task_id].name,
                }
                horus_logger.log.debug(message)
                HorusContext.get_context().bus.emit(
                    HorusTaskEvent(
                        task_id=blocked_id,
                        task_name=blocked_task.name,
                        message=message,
                    )
                )

    if failed:
        raise WorkflowExecutionError(sorted(failed)) from next(
            iter(failed.values())
        )
