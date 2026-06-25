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

import pytest
from rich.console import Console

from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.event.tui_subscriber import WorkflowTUISubscriber
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.status import TaskStatus
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

    def test_render_shows_tasks_and_statuses(self) -> None:
        """The table lists each task name and its colored status."""
        wf = _workflow()
        wf.tasks[0].status = TaskStatus.RUNNING
        wf.tasks[1].status = TaskStatus.COMPLETED

        tui = WorkflowTUISubscriber()
        tui.track(wf)

        console = Console(record=True, width=80)
        console.print(tui.render())
        out = console.export_text()

        assert "Alpha" in out
        assert "Beta" in out
        assert "running" in out
        assert "completed" in out
        assert "demo_wf" in out  # table title is the workflow name

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

    def test_setup_is_noop(self) -> None:
        """setup() is a no-op and must not raise."""
        WorkflowTUISubscriber().setup()
