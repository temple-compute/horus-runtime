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
Live terminal UI for a running workflow.

``WorkflowTUISubscriber`` subscribes to the event bus and repaints a Rich table
of the workflow's tasks and their statuses as events arrive. It is **opt-in**:
the CLI constructs it and attaches it with ``bus.subscribe(...)``. It sets
``add_to_registry = False`` so the bus does not auto-instantiate it on startup
(registry subscribers are created with no arguments).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar

from pydantic import PrivateAttr
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from horus_runtime.context import HorusContext
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.event.base import BaseEvent
from horus_runtime.event.subscriber import BaseEventSubscriber, EventFilterType

#: Rich style per task status.
_STATUS_STYLE: dict[TaskStatus, str] = {
    TaskStatus.IDLE: "dim",
    TaskStatus.PENDING: "cyan",
    TaskStatus.RUNNING: "bold yellow",
    TaskStatus.COMPLETED: "bold green",
    TaskStatus.FAILED: "bold red",
    TaskStatus.CANCELED: "magenta",
}


class WorkflowTUISubscriber(BaseEventSubscriber):
    """
    Render a live table of workflow task statuses from the event bus.
    """

    # Opt-in only: never auto-registered, so bus.start() won't construct it.
    add_to_registry: ClassVar[bool] = False
    subscriber_type: str = "workflow_tui"
    # Subscribe to every event; any event may change a task's status.
    events: ClassVar[EventFilterType] = (BaseEvent,)

    _console: Console = PrivateAttr(default_factory=Console)
    _live: Live | None = PrivateAttr(default=None)
    _workflow: BaseWorkflow | None = PrivateAttr(default=None)

    def setup(self) -> None:
        """No startup work needed."""

    def track(self, workflow: BaseWorkflow) -> None:
        """Render *workflow*'s tasks (so all rows show before the run starts).

        If never called, :meth:`render` falls back to the active workflow on
        the context.
        """
        self._workflow = workflow

    @contextmanager
    def live(self) -> Iterator[None]:
        """Drive a Rich ``Live`` display for the ``with`` body."""
        with Live(
            self.render(),
            console=self._console,
            refresh_per_second=8,
            transient=False,
        ) as live:
            self._live = live
            try:
                yield
            finally:
                # Final repaint so the terminating statuses are shown.
                live.update(self.render())
                self._live = None

    def handle(self, event: BaseEvent) -> None:
        """Repaint the table whenever an event arrives."""
        del event
        if self._live is not None:
            self._live.update(self.render())

    def render(self) -> Table:
        """Build the task-status table from the tracked/active workflow."""
        workflow = self._workflow or HorusContext.get_context().workflow
        table = Table(title=workflow.name if workflow else "Workflow")
        table.add_column("Task", no_wrap=True)
        table.add_column("Status")
        if workflow is not None:
            for task in workflow.tasks:
                style = _STATUS_STYLE.get(task.status, "white")
                table.add_row(task.name, Text(task.status.value, style=style))
        return table
