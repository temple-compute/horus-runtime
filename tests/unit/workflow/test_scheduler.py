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
Unit tests for the concurrent ready-set scheduler.
"""

import asyncio
import json
from collections.abc import Generator
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import AsyncMock, patch

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.dag import (
    StalledWorkflowError,
    ancestors,
    descendants,
)
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_builtin.workflow.scheduler import (
    DEFAULT_MAX_CONCURRENCY,
    TargetPool,
)
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.placement import (
    InsufficientCapacityError,
    ResourceCapacity,
)
from horus_runtime.core.resources import ResourceRequest
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import InputDigests
from horus_runtime.core.task.exceptions import (
    TargetLostError,
    TaskExecutionError,
)
from horus_runtime.core.task.status import SkipReason, TaskStatus
from horus_runtime.core.transfer.events import TransferEvent
from horus_runtime.core.transfer.strategy import BaseTransferStrategy
from horus_runtime.core.workflow.base import EdgeSource
from horus_runtime.core.workflow.condition import EdgeCondition
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowExecutionError
from horus_runtime.core.workflow.status import WorkflowStatus
from horus_runtime.middleware.bound_task import (
    BoundTaskMiddleware,
    BoundTaskMiddlewareContext,
)
from horus_runtime.middleware.task import TaskMiddleware, TaskMiddlewareContext


def _task(
    task_id: str,
    *,
    tmp_path: Path,
    inputs: list[FileArtifact] | None = None,
    outputs: list[FileArtifact] | None = None,
    task_cls: type[HorusTask] = HorusTask,
    target: BaseTarget | None = None,
    resources: ResourceRequest | None = None,
) -> HorusTask:
    """Build a minimal task of *task_cls*, defaulting to a plain HorusTask."""
    return task_cls(
        id=task_id,
        name=task_id,
        inputs=cast(list[BaseArtifact], inputs or []),
        outputs=cast(list[BaseArtifact], outputs or []),
        runtime=CommandRuntime(command=f"echo {task_id}"),
        executor=ShellExecutor(),
        target=target or LocalTarget(working_directory=tmp_path.as_posix()),
        resources=resources,
    )


def _edge(
    source: str, source_output: str, target: str, target_input: str
) -> WorkflowEdge:
    return WorkflowEdge(
        source=source,
        source_output=source_output,
        target=target,
        target_input=target_input,
    )


@pytest.fixture
def restore_bound_task_registry() -> Generator[None]:
    """
    Restore the bound-task middleware registry after each test; a
    subclass defined in a test registers itself.
    """
    original_registry = list(BoundTaskMiddleware.registry)
    try:
        yield
    finally:
        BoundTaskMiddleware.registry = original_registry


@pytest.fixture
def bound_task_calls(
    restore_bound_task_registry: None,
) -> list[tuple[str, bool]]:
    """
    Record each bound-task middleware call as ``(task id, bound)``, where
    ``bound`` means ``ctx.target`` is the task's target, bound to it.
    """
    del restore_bound_task_registry
    calls: list[tuple[str, bool]] = []

    class RecordingMiddleware(BoundTaskMiddleware):
        async def before(self, context: BoundTaskMiddlewareContext) -> None:
            target = context.target
            calls.append(
                (
                    context.task.id,
                    target is context.task.target
                    and target._task is context.task,
                )
            )

    return calls


class _PassTask(HorusTask):
    """
    A task whose ``_run`` does nothing (no real command, no input-artifact
    check). Used as a downstream sink in tests that patch
    ``transfer_artifacts`` to a no-op: with a real ``HorusTask``, the sink
    would fail its own real input-existence check since nothing actually
    materializes the (mocked-away) transfer.
    """

    add_to_registry: ClassVar[bool] = False

    async def _run(self) -> None:
        self.runs += 1


@pytest.mark.unit
class TestConcurrentReadySet:
    """
    The scheduler dispatches every currently-ready task concurrently instead
    of one at a time. ``transfer_artifacts`` is patched to a no-op in most of
    these tests: they exercise scheduling/ordering, not artifact I/O (which
    is covered separately in ``test_builtin_workflow.py`` and
    ``test_edges.py``).
    """

    async def test_two_independent_tasks_reach_running_concurrently(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Two tasks with no dependency on each other, but both required by a
        common downstream sink, must both reach RUNNING before either is
        allowed to finish. Each task blocks on a shared barrier that only
        opens once *both* have started, so a serial scheduler would deadlock
        here (proven by wrapping the run in a timeout) while a concurrent one
        proceeds normally.
        """
        del horus_context
        started: set[str] = set()
        both_started = asyncio.Event()

        class BarrierTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                started.add(self.id)
                if len(started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=5)

        task_a = _task("a", tmp_path=tmp_path, task_cls=BarrierTask)
        task_b = _task("b", tmp_path=tmp_path, task_cls=BarrierTask)
        sink = _task(
            "sink",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            inputs=[
                FileArtifact(id="from_a", path=tmp_path / "a.out"),
                FileArtifact(id="from_b", path=tmp_path / "b.out"),
            ],
        )
        task_a.outputs = [FileArtifact(id="out_a", path=tmp_path / "a.out")]
        task_b.outputs = [FileArtifact(id="out_b", path=tmp_path / "b.out")]

        wf = HorusWorkflow(
            name="concurrent_independent",
            tasks=[task_a, task_b, sink],
            edges=[
                _edge("a", "out_a", "sink", "from_a"),
                _edge("b", "out_b", "sink", "from_b"),
            ],
        )

        with patch.object(
            HorusWorkflow, "transfer_artifacts", new=AsyncMock()
        ):
            await asyncio.wait_for(wf.run(trigger_id="sink"), timeout=10)

        assert started == {"a", "b"}

    async def test_diamond_runs_children_after_parent_and_sink_after_both(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Diamond A -> {B, C} -> D: B and C only start after A finishes, and D
        only starts once both B and C have finished.
        """
        del horus_context
        order: list[str] = []

        class OrderTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                order.append(self.id)

        a = _task(
            "a",
            tmp_path=tmp_path,
            task_cls=OrderTask,
            outputs=[FileArtifact(id="a_out", path=tmp_path / "a.out")],
        )
        b = _task(
            "b",
            tmp_path=tmp_path,
            task_cls=OrderTask,
            inputs=[FileArtifact(id="b_in", path=tmp_path / "a.out")],
            outputs=[FileArtifact(id="b_out", path=tmp_path / "b.out")],
        )
        c = _task(
            "c",
            tmp_path=tmp_path,
            task_cls=OrderTask,
            inputs=[FileArtifact(id="c_in", path=tmp_path / "a.out")],
            outputs=[FileArtifact(id="c_out", path=tmp_path / "c.out")],
        )
        d = _task(
            "d",
            tmp_path=tmp_path,
            task_cls=OrderTask,
            inputs=[
                FileArtifact(id="d_in_b", path=tmp_path / "b.out"),
                FileArtifact(id="d_in_c", path=tmp_path / "c.out"),
            ],
        )

        wf = HorusWorkflow(
            name="diamond",
            tasks=[d, c, b, a],  # definition order deliberately scrambled
            edges=[
                _edge("a", "a_out", "b", "b_in"),
                _edge("a", "a_out", "c", "c_in"),
                _edge("b", "b_out", "d", "d_in_b"),
                _edge("c", "c_out", "d", "d_in_c"),
            ],
        )

        with patch.object(
            HorusWorkflow, "transfer_artifacts", new=AsyncMock()
        ):
            await asyncio.wait_for(wf.run(trigger_id="a"), timeout=10)

        assert order[0] == "a"
        assert order[-1] == "d"
        assert set(order[1:3]) == {"b", "c"}

    async def test_max_concurrency_one_never_overlaps(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        With ``max_concurrency=1``, three tasks that all become ready at the
        same time (siblings fed by a common parent) still never have more
        than one RUNNING at once.
        """
        del horus_context
        state = {"current": 0, "max_seen": 0}

        class ConcurrencyTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                state["current"] += 1
                state["max_seen"] = max(state["max_seen"], state["current"])
                # Yield control so a scheduler that (incorrectly) allowed
                # overlap would have a chance to start a sibling here.
                await asyncio.sleep(0.01)
                state["current"] -= 1

        root = _task(
            "root",
            tmp_path=tmp_path,
            task_cls=ConcurrencyTask,
            outputs=[FileArtifact(id="root_out", path=tmp_path / "root.out")],
        )
        children = [
            _task(
                child_id,
                tmp_path=tmp_path,
                task_cls=ConcurrencyTask,
                inputs=[FileArtifact(id="in", path=tmp_path / "root.out")],
            )
            for child_id in ("b", "c", "e")
        ]

        wf = HorusWorkflow(
            name="fanout",
            tasks=[root, *children],
            edges=[
                _edge("root", "root_out", child.id, "in") for child in children
            ],
            max_concurrency=1,
        )

        with patch.object(
            HorusWorkflow, "transfer_artifacts", new=AsyncMock()
        ):
            await asyncio.wait_for(wf.run(trigger_id="root"), timeout=10)

        assert state["max_seen"] == 1

    async def test_unset_max_concurrency_still_caps_a_wide_fanout(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        With ``max_concurrency`` left unset, a fan-out wider than
        ``DEFAULT_MAX_CONCURRENCY`` never has more than that many siblings
        RUNNING at once (it's not truly unbounded), but more than one still
        runs at a time (it's not serialized to 1 either).
        """
        del horus_context
        state = {"current": 0, "max_seen": 0}

        class ConcurrencyTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                state["current"] += 1
                state["max_seen"] = max(state["max_seen"], state["current"])
                await asyncio.sleep(0.01)
                state["current"] -= 1

        root = _task(
            "root",
            tmp_path=tmp_path,
            task_cls=ConcurrencyTask,
            outputs=[FileArtifact(id="root_out", path=tmp_path / "root.out")],
        )
        children = [
            _task(
                f"child{i}",
                tmp_path=tmp_path,
                task_cls=ConcurrencyTask,
                inputs=[FileArtifact(id="in", path=tmp_path / "root.out")],
            )
            for i in range(DEFAULT_MAX_CONCURRENCY + 4)
        ]

        wf = HorusWorkflow(
            name="wide_fanout_unset_concurrency",
            tasks=[root, *children],
            edges=[
                _edge("root", "root_out", child.id, "in") for child in children
            ],
        )

        with patch.object(
            HorusWorkflow, "transfer_artifacts", new=AsyncMock()
        ):
            await asyncio.wait_for(wf.run(trigger_id="root"), timeout=10)

        assert 1 < state["max_seen"] <= DEFAULT_MAX_CONCURRENCY

    async def test_failure_cancels_concurrent_sibling_and_stops_downstream(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Fail-fast is preserved under concurrency: when one of two
        concurrently-running root tasks fails, the other (still in flight,
        blocked indefinitely) is cancelled, the original exception propagates
        out of ``run()``, and their common downstream sink never starts.
        """
        del horus_context

        class FailingTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                raise TaskExecutionError("boom")

        class BlockingTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                # Blocks forever unless cancelled by the scheduler's
                # fail-fast path.
                await asyncio.Event().wait()

        failing = _task(
            "failing",
            tmp_path=tmp_path,
            task_cls=FailingTask,
            outputs=[FileArtifact(id="f_out", path=tmp_path / "f.out")],
        )
        blocking = _task(
            "blocking",
            tmp_path=tmp_path,
            task_cls=BlockingTask,
            outputs=[FileArtifact(id="b_out", path=tmp_path / "b.out")],
        )
        sink = _task(
            "sink",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            inputs=[
                FileArtifact(id="from_f", path=tmp_path / "f.out"),
                FileArtifact(id="from_b", path=tmp_path / "b.out"),
            ],
        )

        wf = HorusWorkflow(
            name="fail_fast_concurrent",
            tasks=[failing, blocking, sink],
            edges=[
                _edge("failing", "f_out", "sink", "from_f"),
                _edge("blocking", "b_out", "sink", "from_b"),
            ],
        )

        with (
            patch.object(HorusWorkflow, "transfer_artifacts", new=AsyncMock()),
            pytest.raises(TaskExecutionError),
        ):
            await asyncio.wait_for(wf.run(trigger_id="sink"), timeout=10)

        assert blocking.status == TaskStatus.CANCELED
        assert sink.runs == 0

    async def test_external_cancel_kills_executor_and_marks_task_canceled(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Cancelling the workflow run from the outside (as the orchestrator
        does on a user cancel) must propagate to in-flight tasks: the task
        ends CANCELED, the workflow ends CANCELED, and the executor's
        ``cancel_execution`` kill hook fires so external processes
        (containers, remote jobs) are terminated.
        """
        del horus_context
        started = asyncio.Event()
        kill_calls: list[str] = []

        class SpyExecutor(ShellExecutor):
            add_to_registry: ClassVar[bool] = False

            async def cancel_execution(self) -> None:
                kill_calls.append("killed")

        class HangingTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                started.set()
                await asyncio.Event().wait()

        task = _task("hang", tmp_path=tmp_path, task_cls=HangingTask)
        task.executor = SpyExecutor()
        wf = HorusWorkflow(name="external_cancel", tasks=[task], edges=[])

        with patch.object(
            HorusWorkflow, "transfer_artifacts", new=AsyncMock()
        ):
            run = asyncio.create_task(wf.run(trigger_id="hang"))
            await asyncio.wait_for(started.wait(), timeout=5)
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(run, timeout=5)

        assert task.status == TaskStatus.CANCELED
        assert wf.status == WorkflowStatus.CANCELED
        assert kill_calls == ["killed"]

    async def test_cycle_raises_instead_of_stopping_silently(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A cycle expressed through edges leaves the involved tasks
        permanently blocked on each other; the scheduler must raise instead
        of quietly finishing with nothing left running.
        """
        del horus_context
        a = _task(
            "a",
            tmp_path=tmp_path,
            inputs=[FileArtifact(id="a_in", path=tmp_path / "a.in")],
            outputs=[FileArtifact(id="a_out", path=tmp_path / "a.out")],
        )
        b = _task(
            "b",
            tmp_path=tmp_path,
            inputs=[FileArtifact(id="b_in", path=tmp_path / "b.in")],
            outputs=[FileArtifact(id="b_out", path=tmp_path / "b.out")],
        )
        wf = HorusWorkflow(
            name="cyclic",
            tasks=[a, b],
            edges=[
                _edge("a", "a_out", "b", "b_in"),
                _edge("b", "b_out", "a", "a_in"),
            ],
        )

        with pytest.raises(Exception, match="Cycle detected"):
            await asyncio.wait_for(wf.run(trigger_id="a"), timeout=10)

    @staticmethod
    def _join_workflow(
        tmp_path: Path,
    ) -> tuple[HorusWorkflow, list[HorusTask]]:
        """
        Build ``setup -> fetch -> report`` plus ``cleanup -> fetch``:
        ``cleanup`` is neither ancestor nor descendant of ``setup``.
        """
        tasks = [
            _task(t, tmp_path=tmp_path, task_cls=_PassTask)
            for t in ("setup", "fetch", "report", "cleanup")
        ]
        wf = HorusWorkflow(
            name="join",
            tasks=list(tasks),
            edges=[
                WorkflowEdge(source="setup", target="fetch"),
                WorkflowEdge(source="fetch", target="report"),
                WorkflowEdge(source="cleanup", target="fetch"),
            ],
        )
        return wf, tasks

    async def test_trigger_runs_out_of_branch_parent_of_a_join(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Triggering ``setup`` must also run ``cleanup`` (the join's other
        parent), so ``fetch`` and ``report`` run instead of being silently
        skipped.
        """
        del horus_context
        wf, tasks = self._join_workflow(tmp_path)

        await asyncio.wait_for(wf.run(trigger_id="setup"), timeout=10)

        assert [t.runs for t in tasks] == [1, 1, 1, 1]
        assert wf.status == WorkflowStatus.COMPLETED

    async def test_stalled_run_raises_instead_of_completing(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        If tasks in scope can never become ready and no cycle explains it,
        the run fails rather than reporting success. Forced here with the
        old, too-narrow trigger scope that left ``cleanup`` out.
        """
        del horus_context
        wf, tasks = self._join_workflow(tmp_path)

        def narrow_scope(
            trigger_id: str, deps: dict[str, set[str]]
        ) -> set[str]:
            return ancestors(trigger_id, deps) | descendants(trigger_id, deps)

        with (
            patch(
                "horus_builtin.workflow.scheduler.trigger_scope",
                new=narrow_scope,
            ),
            pytest.raises(StalledWorkflowError, match="fetch, report"),
        ):
            await asyncio.wait_for(wf.run(trigger_id="setup"), timeout=10)

        assert tasks[0].runs == 1
        assert tasks[1].runs == 0
        assert wf.status == WorkflowStatus.FAILED

    async def test_shared_target_instance_gets_idle_clone(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Two ready tasks that declare the exact same target *instance* do not
        block each other: the second acquisition gets an idle clone of that
        target instead of waiting for the first to finish, so both tasks
        genuinely overlap.
        """
        del horus_context
        shared_target = LocalTarget(working_directory=tmp_path.as_posix())
        started: set[str] = set()
        both_started = asyncio.Event()
        seen_target_ids: list[int] = []

        class SharedTargetTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                seen_target_ids.append(id(self.target))
                started.add(self.id)
                if len(started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=5)

        task_a = SharedTargetTask(
            id="a",
            name="a",
            outputs=[FileArtifact(id="out_a", path=tmp_path / "a.out")],
            runtime=CommandRuntime(command="echo a"),
            executor=ShellExecutor(),
            target=shared_target,
        )
        task_b = SharedTargetTask(
            id="b",
            name="b",
            outputs=[FileArtifact(id="out_b", path=tmp_path / "b.out")],
            runtime=CommandRuntime(command="echo b"),
            executor=ShellExecutor(),
            target=shared_target,
        )
        sink = _task(
            "sink",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            inputs=[
                FileArtifact(id="from_a", path=tmp_path / "a.out"),
                FileArtifact(id="from_b", path=tmp_path / "b.out"),
            ],
        )

        wf = HorusWorkflow(
            name="shared_target",
            tasks=[task_a, task_b, sink],
            edges=[
                _edge("a", "out_a", "sink", "from_a"),
                _edge("b", "out_b", "sink", "from_b"),
            ],
        )

        with patch.object(
            HorusWorkflow, "transfer_artifacts", new=AsyncMock()
        ):
            await asyncio.wait_for(wf.run(trigger_id="sink"), timeout=10)

        # Both tasks overlapped (the barrier only opens once both started)
        # using two distinct target instances: one is the shared declared
        # target, the other an idle clone minted for the second acquirer.
        assert started == {"a", "b"}
        assert len(set(seen_target_ids)) == 2
        assert id(shared_target) in seen_target_ids
        # The declared target instance itself is restored on task_a/task_b
        # once each finishes running.
        assert task_a.target is shared_target
        assert task_b.target is shared_target


@pytest.mark.unit
class TestFailurePolicy:
    """
    Tests for ``workflow.failure_policy``: ``"fail_fast"`` (the default,
    covered by ``TestConcurrentReadySet.``
    ``test_failure_cancels_concurrent_sibling_and_stops_downstream``) versus
    ``"continue"``.
    """

    def test_default_failure_policy_is_fail_fast(self) -> None:
        """
        A workflow that doesn't set ``failure_policy`` gets the historical
        fail-fast behavior.
        """
        wf = HorusWorkflow(name="default_policy", tasks=[])
        assert wf.failure_policy == "fail_fast"

    async def test_continue_policy_blocks_only_failed_branch(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Under ``failure_policy="continue"``, a failed task's descendants are
        never dispatched, but an unrelated sibling branch fed by the same
        root still runs to completion. The workflow still ends FAILED, via a
        ``WorkflowExecutionError`` naming the failed task.
        """
        del horus_context

        class FailingTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                raise TaskExecutionError("boom")

        root = _task(
            "root",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            outputs=[FileArtifact(id="root_out", path=tmp_path / "root.out")],
        )
        b = _task(
            "b",
            tmp_path=tmp_path,
            task_cls=FailingTask,
            inputs=[FileArtifact(id="b_in", path=tmp_path / "root.out")],
            outputs=[FileArtifact(id="b_out", path=tmp_path / "b.out")],
        )
        b_child = _task(
            "b_child",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            inputs=[FileArtifact(id="child_in", path=tmp_path / "b.out")],
        )
        c = _task(
            "c",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            inputs=[FileArtifact(id="c_in", path=tmp_path / "root.out")],
        )

        wf = HorusWorkflow(
            name="continue_policy",
            tasks=[root, b, b_child, c],
            edges=[
                _edge("root", "root_out", "b", "b_in"),
                _edge("root", "root_out", "c", "c_in"),
                _edge("b", "b_out", "b_child", "child_in"),
            ],
            failure_policy="continue",
        )

        with (
            patch.object(HorusWorkflow, "transfer_artifacts", new=AsyncMock()),
            pytest.raises(WorkflowExecutionError, match="b") as exc_info,
        ):
            await asyncio.wait_for(wf.run(trigger_id="root"), timeout=10)

        assert exc_info.value.failed_task_ids == ["b"]
        assert c.runs == 1
        assert c.status == TaskStatus.COMPLETED
        assert b.status == TaskStatus.FAILED
        # b_child is blocked behind b's failure: it never runs, and says why.
        assert b_child.runs == 0
        assert b_child.status == TaskStatus.SKIPPED
        assert b_child.skip_reason == SkipReason.UPSTREAM_FAILED

        # Running it later clears the stale reason.
        await b_child.run()
        assert b_child.runs == 1
        assert b_child.skip_reason is None
        assert wf.status == WorkflowStatus.FAILED

    async def test_continue_policy_with_no_failures_completes_normally(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        ``failure_policy="continue"`` behaves exactly like the default when
        nothing fails: every task runs to completion, the workflow ends
        COMPLETED, and no ``WorkflowExecutionError`` is raised.
        """
        del horus_context

        a = _task(
            "a",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            outputs=[FileArtifact(id="a_out", path=tmp_path / "a.out")],
        )
        b = _task(
            "b",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            inputs=[FileArtifact(id="b_in", path=tmp_path / "a.out")],
        )

        wf = HorusWorkflow(
            name="continue_no_failures",
            tasks=[a, b],
            edges=[_edge("a", "a_out", "b", "b_in")],
            failure_policy="continue",
        )

        with patch.object(
            HorusWorkflow, "transfer_artifacts", new=AsyncMock()
        ):
            await asyncio.wait_for(wf.run(trigger_id="a"), timeout=10)

        assert a.runs == 1
        assert b.runs == 1
        assert wf.status == WorkflowStatus.COMPLETED


@pytest.mark.unit
class TestResourcePlacement:
    """
    Tests for opt-in, resource/target-aware placement (``workflow.capacity``
    plus a task's ``resources``), layered on top of the concurrent
    ready-set scheduler and its ``TargetPool``.
    """

    async def test_gpu_capacity_caps_concurrency_and_all_tasks_complete(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A location with ``gpus=2`` capacity and four sibling tasks each
        requesting ``gpus=1`` never runs more than two of them at once, yet
        all four eventually complete.
        """
        del horus_context
        current = {"n": 0}
        max_seen = {"n": 0}

        class GpuTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                current["n"] += 1
                max_seen["n"] = max(max_seen["n"], current["n"])
                await asyncio.sleep(0.02)
                current["n"] -= 1

        location_id = LocalTarget(
            working_directory=tmp_path.as_posix()
        ).location_id

        root = _task(
            "root",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            outputs=[FileArtifact(id="root_out", path=tmp_path / "root.out")],
        )
        children = [
            _task(
                child_id,
                tmp_path=tmp_path,
                task_cls=GpuTask,
                inputs=[FileArtifact(id="in", path=tmp_path / "root.out")],
                resources=ResourceRequest(gpus=1),
            )
            for child_id in ("g1", "g2", "g3", "g4")
        ]

        wf = HorusWorkflow(
            name="gpu_fanout",
            tasks=[root, *children],
            edges=[
                _edge("root", "root_out", child.id, "in") for child in children
            ],
            capacity={location_id: ResourceCapacity(gpus=2)},
        )

        with patch.object(
            HorusWorkflow, "transfer_artifacts", new=AsyncMock()
        ):
            await asyncio.wait_for(wf.run(trigger_id="root"), timeout=10)

        assert max_seen["n"] == 2
        assert all(child.status == TaskStatus.COMPLETED for child in children)
        assert wf.status == WorkflowStatus.COMPLETED

    async def test_tasks_without_resources_are_unaffected_by_capacity(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Declaring a location's capacity doesn't gate a task that itself
        declares no ``resources``: it runs exactly as it would with no
        placement configured at all, so several such siblings still overlap.
        """
        del horus_context
        started: set[str] = set()
        all_started = asyncio.Event()

        class BarrierTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                started.add(self.id)
                if len(started) == 3:
                    all_started.set()
                await asyncio.wait_for(all_started.wait(), timeout=5)

        location_id = LocalTarget(
            working_directory=tmp_path.as_posix()
        ).location_id

        root = _task(
            "root",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            outputs=[FileArtifact(id="root_out", path=tmp_path / "root.out")],
        )
        children = [
            _task(
                child_id,
                tmp_path=tmp_path,
                task_cls=BarrierTask,
                inputs=[FileArtifact(id="in", path=tmp_path / "root.out")],
            )
            for child_id in ("c1", "c2", "c3")
        ]

        wf = HorusWorkflow(
            name="unresourced_fanout",
            tasks=[root, *children],
            edges=[
                _edge("root", "root_out", child.id, "in") for child in children
            ],
            # A single-GPU location would deadlock a GPU-requesting fan-out
            # of three, but these children request nothing, so it never
            # applies to them.
            capacity={location_id: ResourceCapacity(gpus=1)},
        )

        with patch.object(
            HorusWorkflow, "transfer_artifacts", new=AsyncMock()
        ):
            await asyncio.wait_for(wf.run(trigger_id="root"), timeout=10)

        assert started == {"c1", "c2", "c3"}

    async def test_request_exceeding_total_capacity_raises_not_hangs(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A task requesting more of a dimension than a location's total
        declared capacity fails fast with a clear error instead of blocking
        the run forever.
        """
        del horus_context
        location_id = LocalTarget(
            working_directory=tmp_path.as_posix()
        ).location_id

        greedy = _task(
            "greedy",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            resources=ResourceRequest(gpus=5),
        )

        wf = HorusWorkflow(
            name="impossible_request",
            tasks=[greedy],
            capacity={location_id: ResourceCapacity(gpus=2)},
        )

        with (
            patch.object(HorusWorkflow, "transfer_artifacts", new=AsyncMock()),
            pytest.raises(InsufficientCapacityError, match="gpus"),
        ):
            await asyncio.wait_for(wf.run(trigger_id="greedy"), timeout=5)


@pytest.mark.unit
class TestPreRunFailureIsAttributed:
    """
    A failure in the setup phase — target acquisition, binding, artifact
    transfer — never reaches ``BaseTask.run``, which is the only place that
    records FAILED and the only thing the task middleware wraps. The scheduler
    has to attribute it to the task itself, or the run reports a bare workflow
    failure with no indication of which task broke.
    """

    async def test_transfer_failure_marks_task_failed_and_emits_event(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        When ``transfer_artifacts`` raises (e.g. an SSH target whose host key
        is not trusted), the consuming task ends FAILED and announces itself
        on the bus, instead of staying IDLE and silent.
        """
        root = _task(
            "root",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            outputs=[FileArtifact(id="root_out", path=tmp_path / "root.out")],
        )
        consumer = _task(
            "consumer",
            tmp_path=tmp_path,
            task_cls=_PassTask,
            inputs=[FileArtifact(id="in", path=tmp_path / "root.out")],
        )
        wf = HorusWorkflow(
            name="transfer_failure",
            tasks=[root, consumer],
            edges=[_edge("root", "root_out", "consumer", "in")],
        )

        async def fail_for_consumer(
            task: HorusTask, _source_map: object
        ) -> None:
            if task.id == "consumer":
                raise TaskExecutionError("host key is not trusted")

        events: list[object] = []
        with (
            patch.object(
                HorusWorkflow,
                "transfer_artifacts",
                new=AsyncMock(side_effect=fail_for_consumer),
            ),
            patch.object(horus_context.bus, "emit", side_effect=events.append),
            pytest.raises(TaskExecutionError, match="host key is not trusted"),
        ):
            await asyncio.wait_for(wf.run(trigger_id="root"), timeout=10)

        # The task never ran, so without the scheduler recording it the status
        # would still be IDLE and the terminal snapshot would name no task.
        assert consumer.runs == 0
        assert consumer.status == TaskStatus.FAILED
        assert any(
            getattr(event, "task_id", None) == "consumer"
            and "host key is not trusted" in getattr(event, "message", "")
            for event in events
        )


class _FarLocalTarget(LocalTarget):
    """
    A local target that pretends to live on its own machine: a distinct
    ``location_id`` keeps transfers from short-circuiting, and artifacts live
    at ``working_directory/<name>`` the way they do on an SSH target.
    """

    add_to_registry = False

    @property
    def location_id(self) -> str:
        return f"far://{self.resolved_working_directory}"

    def path_on_target(self, artifact: BaseArtifact) -> str:
        return f"{self.resolved_working_directory}/{Path(artifact.path).name}"


@pytest.mark.unit
class TestSkipBeforeTransfer:
    """
    A complete task is skipped before its inputs are transferred, judged
    from their digests at the source, so a re-run moves no bytes and emits
    no transfer for it.
    """

    @pytest.fixture(autouse=True)
    def _generic_transfer(self) -> Generator[None]:
        """Move bytes for real: local targets otherwise share a no-op."""
        with patch.object(
            BaseTransferStrategy, "get_from_registry", return_value=None
        ):
            yield

    @staticmethod
    def _build(tmp_path: Path) -> tuple[HorusWorkflow, HorusTask]:
        """``producer`` on one far target feeding ``consumer`` on another."""
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir(exist_ok=True)
        dst.mkdir(exist_ok=True)
        producer = HorusTask(
            id="producer",
            name="producer",
            outputs=[FileArtifact(id="data", path=Path("data.txt"))],
            runtime=CommandRuntime(command=f"echo hi > {src}/data.txt"),
            executor=ShellExecutor(),
            target=_FarLocalTarget(working_directory=src.as_posix()),
        )
        consumer = HorusTask(
            id="consumer",
            name="consumer",
            inputs=[FileArtifact(id="inp", path=Path("data.txt"))],
            outputs=[FileArtifact(id="result", path=Path("result.txt"))],
            runtime=CommandRuntime(
                command=f"cp {dst}/data.txt {dst}/result.txt"
            ),
            executor=ShellExecutor(),
            target=_FarLocalTarget(working_directory=dst.as_posix()),
        )
        wf = HorusWorkflow(
            name="skip_before_transfer",
            tasks=[producer, consumer],
            edges=[_edge("producer", "data", "consumer", "inp")],
        )
        return wf, consumer

    async def test_unchanged_rerun_transfers_and_dispatches_nothing(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Both tasks skip without a TransferEvent or a dispatch."""
        wf, _ = self._build(tmp_path)
        await wf.run(trigger_id="consumer")
        assert wf.status == WorkflowStatus.COMPLETED

        wf, consumer = self._build(tmp_path)
        events: list[object] = []
        with (
            patch.object(horus_context.bus, "emit", side_effect=events.append),
            patch.object(
                _FarLocalTarget, "dispatch", new_callable=AsyncMock
            ) as dispatch,
        ):
            await asyncio.wait_for(wf.run(trigger_id="consumer"), timeout=10)

        assert [t.status for t in wf.tasks] == [TaskStatus.SKIPPED] * 2
        dispatch.assert_not_awaited()
        assert not [e for e in events if isinstance(e, TransferEvent)]
        assert consumer.runs == 0

    async def test_changed_upstream_output_still_transfers_and_runs(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A digest mismatch at the source falls through to a real run."""
        wf, _ = self._build(tmp_path)
        await wf.run(trigger_id="consumer")
        # The producer stays complete (no inputs, same config) but now
        # hands the consumer different bytes.
        (tmp_path / "src" / "data.txt").write_text("changed\n")

        wf, consumer = self._build(tmp_path)
        events: list[object] = []
        with patch.object(
            horus_context.bus, "emit", side_effect=events.append
        ):
            await asyncio.wait_for(wf.run(trigger_id="consumer"), timeout=10)

        assert consumer.status == TaskStatus.COMPLETED
        phases = [
            e.phase
            for e in events
            if isinstance(e, TransferEvent) and e.task_id == "consumer"
        ]
        assert (phases[0], phases[-1]) == ("started", "finished")
        assert (tmp_path / "dst" / "result.txt").read_text() == "changed\n"


@pytest.mark.unit
class TestBoundTaskMiddleware:
    """
    The bound-task middleware wraps each attempt of a placed task, after
    bind and before the pre-transfer completion check, skipped or not.
    """

    _build = staticmethod(TestSkipBeforeTransfer._build)

    @pytest.fixture(autouse=True)
    def _generic_transfer(self) -> Generator[None]:
        """Move bytes for real: local targets otherwise share a no-op."""
        with patch.object(
            BaseTransferStrategy, "get_from_registry", return_value=None
        ):
            yield

    async def test_fires_before_the_check_for_executed_tasks(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        bound_task_calls: list[tuple[str, bool]],
    ) -> None:
        """One call per task, on its bound target, before its check."""
        del horus_context
        wf, _ = self._build(tmp_path)
        order: list[str] = []
        check = HorusTask.is_complete_before_transfer

        class OrderMiddleware(BoundTaskMiddleware):
            async def before(
                self, context: BoundTaskMiddlewareContext
            ) -> None:
                order.append(f"hook {context.task.id}")

        async def checking(
            task: HorusTask, input_digests: InputDigests
        ) -> bool:
            order.append(f"check {task.id}")
            return await check(task, input_digests)

        with patch.object(HorusTask, "is_complete_before_transfer", checking):
            await asyncio.wait_for(wf.run(trigger_id="consumer"), timeout=10)

        assert [t.status for t in wf.tasks] == [TaskStatus.COMPLETED] * 2
        assert bound_task_calls == [("producer", True), ("consumer", True)]
        assert order == [
            "hook producer",
            "check producer",
            "hook consumer",
            "check consumer",
        ]

    async def test_fires_for_skipped_tasks(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        bound_task_calls: list[tuple[str, bool]],
    ) -> None:
        """A re-run skips both tasks and still calls the hook for each."""
        del horus_context
        wf, _ = self._build(tmp_path)
        await wf.run(trigger_id="consumer")
        bound_task_calls.clear()

        wf, _ = self._build(tmp_path)
        with patch.object(
            _FarLocalTarget, "dispatch", new_callable=AsyncMock
        ) as dispatch:
            await asyncio.wait_for(wf.run(trigger_id="consumer"), timeout=10)

        assert [t.status for t in wf.tasks] == [TaskStatus.SKIPPED] * 2
        dispatch.assert_not_awaited()
        assert bound_task_calls == [("producer", True), ("consumer", True)]

    async def test_restore_in_before_makes_the_task_skip(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        restore_bound_task_registry: None,
    ) -> None:
        """
        A task seeded as a cache hit, its output gone from the target, skips
        when ``before`` puts the output back, and re-executes without it.
        """
        del horus_context, restore_bound_task_registry
        wf, _ = self._build(tmp_path)
        await wf.run(trigger_id="consumer")
        result = tmp_path / "dst" / "result.txt"

        class RestoringMiddleware(BoundTaskMiddleware):
            async def before(
                self, context: BoundTaskMiddlewareContext
            ) -> None:
                if context.task.id == "consumer":
                    result.write_text("hi\n")

        async def rerun() -> HorusTask:
            result.unlink()
            wf, consumer = self._build(tmp_path)
            consumer.status = TaskStatus.SKIPPED
            consumer.skip_reason = SkipReason.COMPLETE
            await asyncio.wait_for(wf.run(trigger_id="consumer"), timeout=10)
            return consumer

        consumer = await rerun()
        assert consumer.status == TaskStatus.SKIPPED
        assert consumer.runs == 0

        BoundTaskMiddleware.registry.remove(RestoringMiddleware)
        consumer = await rerun()
        assert consumer.status == TaskStatus.COMPLETED
        assert consumer.runs == 1

    @pytest.mark.parametrize("stale", [False, True])
    async def test_manifest_checked_before_restore(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        restore_bound_task_registry: None,
        stale: bool,
    ) -> None:
        """
        ``before`` compares a cached manifest with ``context.input_digests``
        and restores only on a match. The check reuses the same digests.
        """
        del horus_context, restore_bound_task_registry
        wf, _ = self._build(tmp_path)
        await wf.run(trigger_id="consumer")
        result = tmp_path / "dst" / "result.txt"
        manifest_path = tmp_path / "dst" / ".horus" / "consumer.json"
        cached = json.loads(manifest_path.read_text())
        if stale:
            cached["config_hash"] = "changed"
        cached_bytes = json.dumps(cached).encode()
        result.unlink()
        manifest_path.unlink()
        matches: list[bool] = []

        class RestoringMiddleware(BoundTaskMiddleware):
            async def before(
                self, context: BoundTaskMiddlewareContext
            ) -> None:
                if context.task.id != "consumer":
                    return
                match = await context.task.manifest_matches(
                    cached_bytes, context.input_digests
                )
                matches.append(match)
                if match:
                    result.write_text("hi\n")
                    manifest_path.write_bytes(cached_bytes)

        digested: list[str] = []
        source_digests = HorusWorkflow.source_digests

        async def counting(
            workflow: HorusWorkflow,
            task: HorusTask,
            source_map: dict[tuple[str, str], EdgeSource] | None = None,
        ) -> dict[str, str | None]:
            digested.append(task.id)
            return await source_digests(workflow, task, source_map)

        wf, consumer = self._build(tmp_path)
        with patch.object(HorusWorkflow, "source_digests", counting):
            await asyncio.wait_for(wf.run(trigger_id="consumer"), timeout=10)

        assert matches == [not stale]
        assert digested.count("consumer") == 1
        if stale:
            assert consumer.status == TaskStatus.COMPLETED
            assert consumer.runs == 1
        else:
            assert consumer.status == TaskStatus.SKIPPED
            assert consumer.runs == 0

    async def test_fires_with_skip_if_complete_false(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        bound_task_calls: list[tuple[str, bool]],
    ) -> None:
        """A task that never skips still gets one call, and runs."""
        del horus_context
        wf, _ = self._build(tmp_path)
        await wf.run(trigger_id="consumer")
        bound_task_calls.clear()

        wf, consumer = self._build(tmp_path)
        consumer.skip_if_complete = False
        await asyncio.wait_for(wf.run(trigger_id="consumer"), timeout=10)

        assert consumer.status == TaskStatus.COMPLETED
        assert consumer.runs == 1
        assert bound_task_calls == [("producer", True), ("consumer", True)]

    async def test_inactive_branch_does_not_fire(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        bound_task_calls: list[tuple[str, bool]],
    ) -> None:
        """A task on an untaken branch is never placed, so no call."""
        del horus_context
        decision = tmp_path / "check.json"
        decision.write_text(json.dumps({"branch": "b"}))
        check = HorusTask(
            id="check",
            name="check",
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            outputs=[FileArtifact(id="decision", path=decision)],
        )
        untaken = _task("c", tmp_path=tmp_path)
        wf = HorusWorkflow(
            name="branch",
            tasks=[check, untaken],
            edges=[
                WorkflowEdge(
                    source="check",
                    target="c",
                    condition=EdgeCondition(
                        source_task="check",
                        source_output="decision",
                        key="branch",
                        op="eq",
                        value="c",
                    ),
                )
            ],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        await asyncio.wait_for(wf.run(trigger_id="check"), timeout=10)

        assert untaken.skip_reason is SkipReason.INACTIVE
        assert bound_task_calls == [("check", True)]

    async def test_error_fails_the_task(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        restore_bound_task_registry: None,
    ) -> None:
        """An error in ``before`` fails the task and, fail-fast, the run."""
        del restore_bound_task_registry

        class BrokenMiddleware(BoundTaskMiddleware):
            async def before(
                self, context: BoundTaskMiddlewareContext
            ) -> None:
                del context
                raise RuntimeError("restore broke")

        task = _task("t", tmp_path=tmp_path, task_cls=_PassTask)
        wf = HorusWorkflow(name="broken", tasks=[task])
        events: list[object] = []
        with (
            patch.object(horus_context.bus, "emit", side_effect=events.append),
            pytest.raises(RuntimeError, match="restore broke"),
        ):
            await asyncio.wait_for(wf.run(trigger_id="t"), timeout=10)

        assert task.runs == 0
        assert task.status == TaskStatus.FAILED
        assert wf.status == WorkflowStatus.FAILED
        assert any(
            "restore broke" in getattr(event, "message", "")
            for event in events
        )


async def test_zero_max_concurrency_never_waits() -> None:
    """``max_concurrency=0`` hands out a slot per dispatch, with no cap."""
    target = LocalTarget()
    pool = TargetPool(0)
    held = [
        await asyncio.wait_for(pool.acquire(target), timeout=1)
        for _ in range(DEFAULT_MAX_CONCURRENCY * 4)
    ]
    assert not pool.full
    for slot in held:
        pool.release(target, slot)


def test_negative_max_concurrency_is_rejected() -> None:
    """Only ``0`` and up are valid; ``0`` is the no-limit value."""
    with pytest.raises(ValueError):
        HorusWorkflow(name="w", tasks=[], max_concurrency=-1)


class _LosingTask(HorusTask):
    """Loses its machine on each of its first ``losses`` runs."""

    add_to_registry: ClassVar[bool] = False
    losses: int = 0

    async def _run(self) -> None:
        self.runs += 1
        if self.runs <= self.losses:
            raise TargetLostError("node gone")


class _LosingMapTask(_LosingTask):
    """Stands in for a map: it dispatches children."""

    dispatches_children: ClassVar[bool] = True


class _LosingCheckTask(_LosingTask):
    """Loses its machine in its first pre-transfer completion check."""

    add_to_registry: ClassVar[bool] = False
    checks: int = 0

    async def is_complete_before_transfer(
        self, input_digests: InputDigests
    ) -> bool:
        self.checks += 1
        if self.checks == 1:
            raise TargetLostError("node gone")
        return await super().is_complete_before_transfer(input_digests)


class _Node(LocalTarget):
    """A releasable machine that runs one member at a time."""

    add_to_registry = False
    releasable: ClassVar[bool] = True

    @property
    def member_cap(self) -> int | None:
        return 1

    @property
    def is_live(self) -> bool:
        return True

    @property
    def hold_timeout_s(self) -> float:
        return 30.0


@pytest.mark.unit
class TestRetryOnTargetLoss:
    """
    A ``TargetLostError`` re-runs the task once on a new machine. Any other
    failure, or a second loss, fails it as before.
    """

    async def _run_one(
        self,
        tmp_path: Path,
        *,
        losses: int,
        task_cls: type[_LosingTask] = _LosingTask,
    ) -> _LosingTask:
        task = _task("t", tmp_path=tmp_path, task_cls=task_cls)
        assert isinstance(task, _LosingTask)
        task.losses = losses
        wf = HorusWorkflow(name="retry", tasks=[task])
        await asyncio.wait_for(wf.run(trigger_id="t"), timeout=10)
        return task

    async def test_lost_once_runs_twice_and_completes(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The retry runs on the new machine and the task completes."""
        events: list[object] = []
        with patch.object(
            horus_context.bus, "emit", side_effect=events.append
        ):
            task = await self._run_one(tmp_path, losses=1)

        assert task.runs == 2
        assert task.status == TaskStatus.COMPLETED
        assert task.error is None
        assert any(
            "Node lost; retrying t" in getattr(event, "message", "")
            for event in events
        )

    async def test_hook_fires_once_per_node_attempt(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        bound_task_calls: list[tuple[str, bool]],
    ) -> None:
        """One loss, two attempts: the hook runs on each node, bound."""
        del horus_context
        task = await self._run_one(tmp_path, losses=1)

        assert task.runs == 2
        assert task.status == TaskStatus.COMPLETED
        assert bound_task_calls == [("t", True), ("t", True)]

    async def test_loss_in_the_hook_retries_on_a_new_node(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        bound_task_calls: list[tuple[str, bool]],
    ) -> None:
        """A node lost in ``before`` (mid-restore) is retried like a run."""
        lost: list[bool] = []

        class LosingMiddleware(BoundTaskMiddleware):
            async def before(
                self, context: BoundTaskMiddlewareContext
            ) -> None:
                del context
                if not lost:
                    lost.append(True)
                    raise TargetLostError("node gone")

        events: list[object] = []
        with patch.object(
            horus_context.bus, "emit", side_effect=events.append
        ):
            task = await self._run_one(tmp_path, losses=0)

        assert task.runs == 1
        assert task.status == TaskStatus.COMPLETED
        assert task._target_loss_retries == 1
        assert bound_task_calls == [("t", True), ("t", True)]
        assert any(
            "Node lost; retrying t" in getattr(event, "message", "")
            for event in events
        )

    async def test_loss_in_the_check_retries_on_a_new_node(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        bound_task_calls: list[tuple[str, bool]],
    ) -> None:
        """
        A node lost in the pre-transfer check is retried: the hook and the
        check run again on the new node.
        """
        del horus_context
        task = await self._run_one(
            tmp_path, losses=0, task_cls=_LosingCheckTask
        )

        assert isinstance(task, _LosingCheckTask)
        assert task.checks == 2
        assert task.runs == 1
        assert task.status == TaskStatus.COMPLETED
        assert bound_task_calls == [("t", True), ("t", True)]

    async def test_lost_twice_fails(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The task ran twice, once per allowed attempt, and ends FAILED."""
        del horus_context
        task = _task("t", tmp_path=tmp_path, task_cls=_LosingTask)
        assert isinstance(task, _LosingTask)
        task.losses = 2
        wf = HorusWorkflow(name="retry", tasks=[task])
        with pytest.raises(TargetLostError):
            await asyncio.wait_for(wf.run(trigger_id="t"), timeout=10)

        assert task.runs == 2
        assert task.status == TaskStatus.FAILED
        assert task.error == "TargetLostError: node gone"

        await task.reset()
        assert task._target_loss_retries == 0
        assert not task._retry_on_target_loss

    async def test_the_retry_pulls_its_inputs_again(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The lease manager sees the retry pulling, so no machine holding one
        of its inputs is released under it.
        """
        del horus_context
        task = _task("t", tmp_path=tmp_path, task_cls=_LosingTask)
        assert isinstance(task, _LosingTask)
        task.losses = 1
        wf = HorusWorkflow(name="retry", tasks=[task])
        leases = wf.leases
        pulling: list[bool] = []
        transferred = leases.inputs_transferred

        def record(task: HorusTask) -> None:
            pulling.append(
                "t" in leases._pulling and "t" not in leases._inputs_done
            )
            transferred(task)

        with patch.object(leases, "inputs_transferred", side_effect=record):
            await asyncio.wait_for(wf.run(trigger_id="t"), timeout=10)

        assert task.runs == 2
        assert pulling == [True, True]

    async def test_each_run_has_its_own_budget(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A workflow run again gets a fresh retry, and the retry flag does
        not outlive the attempt.
        """
        del horus_context
        task = _task("t", tmp_path=tmp_path, task_cls=_LosingTask)
        assert isinstance(task, _LosingTask)
        task.losses = 1
        wf = HorusWorkflow(name="retry", tasks=[task])
        await asyncio.wait_for(wf.run(trigger_id="t"), timeout=10)
        assert not task._retry_on_target_loss

        task.losses = 3
        await asyncio.wait_for(wf.run(trigger_id="t"), timeout=10)

        assert task.runs == 4
        assert task.status == TaskStatus.COMPLETED

    async def test_plain_failure_runs_once(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A real task failure is not node loss and is never retried."""
        del horus_context

        class FailingTask(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                self.runs += 1
                raise TaskExecutionError("exit code 1")

        task = _task("t", tmp_path=tmp_path, task_cls=FailingTask)
        wf = HorusWorkflow(name="plain", tasks=[task])
        with pytest.raises(TaskExecutionError, match="exit code 1"):
            await asyncio.wait_for(wf.run(trigger_id="t"), timeout=10)

        assert task.runs == 1
        assert task.status == TaskStatus.FAILED

    async def test_map_is_retried_on_its_own_loss(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A loss of the map's own, after its fan-out, is retried once."""
        del horus_context
        task = _task("t", tmp_path=tmp_path, task_cls=_LosingMapTask)
        assert isinstance(task, _LosingTask)
        task.losses = 1
        wf = HorusWorkflow(name="map", tasks=[task])
        await asyncio.wait_for(wf.run(trigger_id="t"), timeout=10)

        assert task.runs == 2
        assert task.status == TaskStatus.COMPLETED

    async def test_no_failed_status_before_the_retry(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A middleware reporting status on its way out sees ``PENDING`` for
        the lost run, never ``FAILED``.
        """
        del horus_context
        seen: list[TaskStatus] = []
        original_registry = list(TaskMiddleware.registry)

        class RecordingMiddleware(TaskMiddleware):
            add_to_registry: ClassVar[bool] = False

            async def after(self, context: TaskMiddlewareContext) -> None:
                seen.append(context.task.status)

        TaskMiddleware.registry = [*original_registry, RecordingMiddleware]
        try:
            task = await self._run_one(tmp_path, losses=1)
        finally:
            TaskMiddleware.registry = original_registry

        assert seen == [TaskStatus.PENDING, TaskStatus.COMPLETED]
        assert task.status == TaskStatus.COMPLETED

    async def test_member_cap_one_keeps_slots_balanced(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Two members of a one-slot machine, each lost once, retry inside
        their slot and placement reservation: no deadlock, and both are
        back once the run ends.
        """
        del horus_context
        node = _Node(working_directory=tmp_path.as_posix())
        location_id = node.location_id
        tasks = [
            _task(
                task_id,
                tmp_path=tmp_path,
                task_cls=_LosingTask,
                target=node,
                resources=ResourceRequest(gpus=1),
            )
            for task_id in ("a", "b")
        ]
        root = _task("root", tmp_path=tmp_path, task_cls=_PassTask)
        for task in tasks:
            assert isinstance(task, _LosingTask)
            task.losses = 1
        wf = HorusWorkflow(
            name="capped",
            tasks=[root, *tasks],
            edges=[
                WorkflowEdge(source="root", target=task.id) for task in tasks
            ],
            capacity={location_id: ResourceCapacity(gpus=1)},
        )
        slots: list[asyncio.Semaphore] = []
        member_slot = wf.leases.member_slot

        async def record_slot(task: HorusTask) -> None:
            await member_slot(task)
            lease = wf.leases._lease_for(task.target)
            if lease is not None and lease.slots is not None:
                slots.append(lease.slots)

        with patch.object(wf.leases, "member_slot", side_effect=record_slot):
            await asyncio.wait_for(wf.run(trigger_id="root"), timeout=10)

        assert [task.runs for task in tasks] == [2, 2]
        assert all(task.status == TaskStatus.COMPLETED for task in tasks)
        assert slots
        assert all(slot._value == 1 for slot in slots)
        assert wf.placement._remaining[location_id] == {"gpus": 1}
