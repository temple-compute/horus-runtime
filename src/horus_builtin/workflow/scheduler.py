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

from horus_builtin.workflow.dag import (
    UnknownTaskError,
    ancestors,
    build_dependencies,
    descendants,
    topological_sort,
)
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.workflow.base import BaseWorkflow, _EdgeSource
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
) -> None:
    """
    Run one ready task to completion: acquire a target, bind, transfer
    inputs, dispatch, and wait, mirroring the per-task body of the previous
    serial loop, but on whichever target the pool hands back (the task's own
    declared target in the common case, or an idle clone under contention).
    """
    declared_target = task.target
    target = await pool.acquire(declared_target)
    # `transfer_artifacts` and `dispatch` both operate off `task.target`, so
    # point it at the target we actually acquired for the duration of the
    # run and restore it afterwards, leaving the task's declared target
    # untouched for any subsequent run of the same workflow instance.
    task.target = target
    try:
        # Associate the task with its target before any transfer so
        # resource-aware targets (which may provision lazily at transfer
        # time, before dispatch) can read task.resources.
        target.bind(task)

        # Transfer input artifacts to the task's target as needed.
        await workflow.transfer_artifacts(task, source_map)

        # Execute the task on its target and wait for it to finish.
        await target.dispatch(task)
        await target.wait()
    finally:
        task.target = declared_target
        pool.release(declared_target, target)


def _collect_completions(
    done: set[asyncio.Task[None]],
    running: dict[asyncio.Task[None], str],
    completed: set[str],
) -> BaseException | None:
    """
    Pop every finished task out of *running*, adding successes to
    *completed*, and return the first exception encountered (if any) in
    deterministic (task id) order.
    """
    failure: BaseException | None = None
    for fut in sorted(done, key=lambda f: running[f]):
        task_id = running.pop(fut)
        if fut.cancelled():
            continue
        exc = fut.exception()
        if exc is not None:
            failure = failure or exc
        else:
            completed.add(task_id)
    return failure


async def _cancel_running(running: dict[asyncio.Task[None], str]) -> None:
    """Cancel every still-running wrapper task and wait for it to unwind."""
    for fut in running:
        fut.cancel()
    if running:
        await asyncio.wait(list(running))
    running.clear()


async def run_schedule(workflow: BaseWorkflow, trigger_id: str) -> None:
    """
    Execute *workflow* from *trigger_id* with a concurrent ready-set
    scheduler.

    A task is skipped when all of its output artifacts exist (see
    ``BaseTask.is_complete``, applied inside ``BaseTask.run``). Every task
    whose dependencies are already satisfied is dispatched immediately and
    concurrently with any other ready task, bounded by
    ``workflow.max_concurrency`` when set. The first task to fail cancels
    every other task still in flight and re-raises, so ``BaseWorkflow.run``
    can mark the workflow ``FAILED`` — the same fail-fast contract the
    previous serial loop provided.

    Raises:
        UnknownTaskError: If trigger_id is not a task in the workflow.
        CyclicDependencyError: If the tasks in scope cannot all be
            scheduled (a cycle keeps the remainder permanently blocked).
    """
    tasks_by_id = {task.id: task for task in workflow.tasks}
    if trigger_id not in tasks_by_id:
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

    # The source map depends only on workflow structure. It is rebuilt only
    # when the workflow's revision advances (nothing bumps it yet, so this
    # effectively builds once, the first time it's needed).
    source_map_cache: tuple[int, dict[tuple[str, str], _EdgeSource]] | None = (
        None
    )

    completed: set[str] = set()
    dispatched: set[str] = set()
    running: dict[asyncio.Task[None], str] = {}

    while True:
        # Recomputed every iteration (instead of once up front) so a
        # DAG-mutation can grow `workflow.tasks`/`workflow.edges` mid-run.
        deps = build_dependencies(workflow.tasks, workflow.edges)
        scope = ancestors(trigger_id, deps) | descendants(trigger_id, deps)

        # Deterministic order when several tasks become ready at once,
        # matching topological_sort's tie-breaking.
        ready = sorted(
            task_id
            for task_id in scope
            if task_id not in dispatched and deps[task_id] <= completed
        )

        if not ready and not running and (scope - completed):
            # Nothing is ready and nothing is in flight, yet the scope isn't
            # fully satisfied: the remaining tasks are stuck on each other.
            # Reuse topological_sort's cycle detection for a consistent
            # error instead of silently stopping short.
            topological_sort(scope, deps)

        if ready:
            source_map_cache = workflow.cached_source_map(source_map_cache)
            source_map = source_map_cache[1]
            for task_id in ready:
                task = tasks_by_id[task_id]
                dispatched.add(task_id)
                wrapper = asyncio.create_task(
                    _execute_ready_task(workflow, task, source_map, pool)
                )
                running[wrapper] = task_id

        if not running:
            break

        done, _pending = await asyncio.wait(
            running, return_when=asyncio.FIRST_COMPLETED
        )

        failure = _collect_completions(done, running, completed)
        if failure is not None:
            await _cancel_running(running)
            raise failure
