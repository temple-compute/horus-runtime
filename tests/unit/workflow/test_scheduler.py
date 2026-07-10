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
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.edge import WorkflowEdge


def _task(
    task_id: str,
    *,
    tmp_path: Path,
    inputs: list[FileArtifact] | None = None,
    outputs: list[FileArtifact] | None = None,
    task_cls: type[HorusTask] = HorusTask,
) -> HorusTask:
    """Build a minimal task of *task_cls*, defaulting to a plain HorusTask."""
    return task_cls(
        id=task_id,
        name=task_id,
        inputs=cast(list[BaseArtifact], inputs or []),
        outputs=cast(list[BaseArtifact], outputs or []),
        runtime=CommandRuntime(command=f"echo {task_id}"),
        executor=ShellExecutor(),
        target=LocalTarget(working_directory=tmp_path.as_posix()),
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
