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
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import AsyncMock, patch

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.placement import (
    InsufficientCapacityError,
    ResourceCapacity,
)
from horus_runtime.core.resources import ResourceRequest
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowExecutionError
from horus_runtime.core.workflow.status import WorkflowStatus


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
        # b_child is blocked behind b's failure: it never even reaches
        # RUNNING, let alone executes.
        assert b_child.runs == 0
        assert b_child.status == TaskStatus.IDLE
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
        assert all(child.runs == 1 for child in children)
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
