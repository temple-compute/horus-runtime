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
Unit tests for the live workflow TUI subscriber.
"""

from pathlib import Path

import pytest
from rich.console import Console

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.event.tui_subscriber import WorkflowTUISubscriber
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.core.interaction.transport import (
    InteractionAnsweredEvent,
    InteractionAskedEvent,
)
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.event.subscriber import BaseEventSubscriber


def _workflow() -> HorusWorkflow:
    """A tiny two-task workflow for rendering."""
    tasks: list[BaseTask] = [
        HorusTask(
            id=tid,
            name=name,
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
        )
        for tid, name in (("a", "Alpha"), ("b", "Beta"))
    ]
    return HorusWorkflow(name="demo_wf", tasks=tasks)


@pytest.mark.unit
class TestWorkflowTUISubscriber:
    """Tests for ``WorkflowTUISubscriber``."""

    def test_not_auto_registered(self) -> None:
        """It must stay out of the auto-instantiated registry."""
        registered = BaseEventSubscriber.registry.values()
        assert WorkflowTUISubscriber not in registered

    def test_render_shows_tasks_and_workflow(self) -> None:
        """Dashboard names each task, the workflow, and a progress total."""
        wf = _workflow()
        wf.tasks[0].status = TaskStatus.RUNNING
        wf.tasks[1].status = TaskStatus.COMPLETED

        tui = WorkflowTUISubscriber()
        tui.track(wf)

        console = Console(record=True, width=120)
        console.print(tui.render())
        out = console.export_text()

        assert "Alpha" in out
        assert "Beta" in out
        assert "demo_wf" in out  # header shows the workflow name
        assert "tasks" in out  # progress label
        assert "✓" in out  # completed glyph

    def test_render_without_workflow_is_safe(self) -> None:
        """render() must not raise when nothing is tracked or active."""
        Console(record=True).print(WorkflowTUISubscriber().render())

    def test_handle_without_live_is_noop(self) -> None:
        """handle() must not raise when no Live display is active."""
        tui = WorkflowTUISubscriber()
        tui.track(_workflow())
        tui.handle(HorusTaskEvent(task_name="Alpha"))

    def test_handle_within_live_repaints(self) -> None:
        """Inside the Live context, handle() repaints without error."""
        tui = WorkflowTUISubscriber()
        tui.track(_workflow())
        with tui.live():
            tui.handle(HorusTaskEvent(task_name="Alpha"))

    def test_interaction_pauses_and_resumes_live(self) -> None:
        """An asked interaction stops the Live; the answer restarts it."""
        tui = WorkflowTUISubscriber()
        tui.track(_workflow())
        asked = InteractionAskedEvent(
            interaction_kind="string",
            transport_kind="cli",
            renderer_key="cli.string",
            value_key="v",
        )
        answered = InteractionAnsweredEvent(
            interaction_kind="string", transport_kind="cli", value_key="v"
        )
        with tui.live():
            assert tui._paused is False
            tui.handle(asked)
            assert tui._paused is True
            tui.handle(answered)
            assert tui._paused is False

    def test_progress_total_follows_a_growing_dag(self) -> None:
        """
        Tasks added after ``track()`` count towards the total.

        A ``loop:``/``sub:`` expander only adds its injected tasks once it
        runs, so a scope planned before the run reported ``1/1 tasks`` for a
        multi-iteration loop.
        """
        wf = _workflow()
        wf.edges.append(WorkflowEdge(source="a", target="b"))
        tui = WorkflowTUISubscriber()
        tui.track(wf, trigger_id="a")

        console = Console(record=True, width=120)
        console.print(tui.render())
        assert "0/2 " in console.export_text()

        wf.tasks.append(
            HorusTask(
                id="c",
                name="Gamma",
                runtime=CommandRuntime(command="true"),
                executor=ShellExecutor(),
            )
        )
        wf.edges.append(WorkflowEdge(source="b", target="c"))

        console = Console(record=True, width=120)
        console.print(tui.render())
        assert "0/3 " in console.export_text()

    def test_setup_is_noop(self) -> None:
        """setup() is a no-op and must not raise."""
        WorkflowTUISubscriber().setup()

    def test_fanout_children_render_before_the_real_successor(self) -> None:
        """A task's own fan-out (wired by an ordering-only, transfer=False
        edge -- e.g. a horus_map's clones, appended at the end of
        ``workflow.tasks`` by ``expand()``) lists right after it, ahead of
        a genuine downstream consumer reached by a real data edge -- not
        wherever the raw task list happens to put it.
        """
        tasks: list[BaseTask] = [
            HorusTask(
                id="map",
                name="Map",
                runtime=CommandRuntime(command="true"),
                executor=ShellExecutor(),
                outputs=[FileArtifact(id="scaled", path=Path("scaled.json"))],
            ),
            HorusTask(
                id="report",
                name="Report",
                runtime=CommandRuntime(command="true"),
                executor=ShellExecutor(),
                inputs=[
                    FileArtifact(id="scaled", path=Path("scaled_in.json"))
                ],
            ),
            *(
                HorusTask(
                    id=tid,
                    name=name,
                    runtime=CommandRuntime(command="true"),
                    executor=ShellExecutor(),
                )
                for tid, name in (("clone0", "Clone 0"), ("clone1", "Clone 1"))
            ),
        ]
        wf = HorusWorkflow(
            name="wf",
            tasks=tasks,
            edges=[
                WorkflowEdge(
                    source="map",
                    source_output="scaled",
                    target="report",
                    target_input="scaled",
                ),
                WorkflowEdge(source="map", target="clone0", transfer=False),
                WorkflowEdge(source="map", target="clone1", transfer=False),
            ],
        )

        tui = WorkflowTUISubscriber()
        tui.track(wf)

        console = Console(record=True, width=120)
        console.print(tui.render())
        out = console.export_text()

        assert (
            out.index("Clone 0") < out.index("Clone 1") < out.index("Report")
        )
