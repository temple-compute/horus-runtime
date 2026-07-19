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
Live terminal dashboard for a running workflow.

``WorkflowTUISubscriber`` is an **opt-in** event-bus subscriber that renders a
live Rich dashboard while a workflow runs: a header (workflow status + wall
clock), a task progress bar, a per-task table beside the dependency DAG, a
scrolling log/event pane, and a failure panel.
"""

import asyncio
import sys
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar, NamedTuple

from pydantic import PrivateAttr
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from horus_builtin.event.artifact_event import ArtifactEvent
from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.event.workflow_event import HorusWorkflowEvent
from horus_builtin.workflow.dag import build_dependencies, execution_plan
from horus_runtime.context import HorusContext
from horus_runtime.core.interaction.transport import (
    InteractionAnsweredEvent,
    InteractionAskedEvent,
    InteractionFailedEvent,
)
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.core.workflow.status import WorkflowStatus
from horus_runtime.event.base import BaseEvent
from horus_runtime.event.subscriber import BaseEventSubscriber, EventFilterType
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

if TYPE_CHECKING:
    from loguru import Message


#: Rich style per task status.
_STATUS_STYLE: dict[TaskStatus, str] = {
    TaskStatus.IDLE: "dim",
    TaskStatus.PENDING: "cyan",
    TaskStatus.RUNNING: "bold yellow",
    TaskStatus.COMPLETED: "bold green",
    TaskStatus.FAILED: "bold red",
    TaskStatus.CANCELED: "magenta",
    TaskStatus.SKIPPED: "blue",
}

# Glyph per task status (RUNNING uses an animated spinner instead).
_STATUS_GLYPH: dict[TaskStatus, str] = {
    TaskStatus.IDLE: "◌",
    TaskStatus.PENDING: "◔",
    TaskStatus.RUNNING: "●",
    TaskStatus.COMPLETED: "✓",
    TaskStatus.FAILED: "✗",
    TaskStatus.CANCELED: "⊘",
    TaskStatus.SKIPPED: "→",
}

# Rich style per workflow status.
_WF_STATUS_STYLE: dict[WorkflowStatus, str] = {
    WorkflowStatus.IDLE: "dim",
    WorkflowStatus.QUEUED: "cyan",
    WorkflowStatus.RUNNING: "bold yellow",
    WorkflowStatus.COMPLETED: "bold green",
    WorkflowStatus.FAILED: "bold red",
    WorkflowStatus.CANCELED: "magenta",
    WorkflowStatus.PARTIAL: "yellow",
}

# Verify all statuses are covered.
assert _STATUS_STYLE.keys() == set(TaskStatus), (
    f"TUI - missing styles for: {set(TaskStatus) - _STATUS_STYLE.keys()}"
)
assert _STATUS_GLYPH.keys() == set(TaskStatus), (
    f"TUI - missing glyphs for: {set(TaskStatus) - _STATUS_GLYPH.keys()}"
)
assert _WF_STATUS_STYLE.keys() == set(WorkflowStatus), (
    "TUI - missing styles for: "
    f"{set(WorkflowStatus) - _WF_STATUS_STYLE.keys()}"
)


# Rich style per loguru/event level, for the log pane.
_LEVEL_STYLE: dict[str, str] = {
    "CRITICAL": "bold red",
    "ERROR": "red",
    "WARNING": "yellow",
    "INFO": "white",
    "DEBUG": "dim",
    "TRACE": "dim",
}

# Tasks in a terminal state count as "executed" for the progress bar.
_TERMINAL: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELED,
        TaskStatus.SKIPPED,
    }
)

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SECONDS_PER_MINUTE = 60

# How many log lines to show in the pane, and how long (seconds) the transient
# "transferring artifact" indicator stays visible after the last event.
_LOG_LINES = 8
_TRANSFER_LINGER_S = 2.0


class _LogLine(NamedTuple):
    """One rendered entry in the log/event pane."""

    when: float  # epoch seconds, for HH:MM:SS formatting
    style: str
    icon: str
    text: str


def _make_console() -> Console:
    """
    Console bound to the *real* stdout.

    Binding to ``sys.__stdout__`` keeps ``is_terminal`` stable and writes
    frames to the real terminal.
    """
    return Console(file=sys.__stdout__ or sys.stdout)


def _spinner_frame() -> str:
    """Pick a spinner glyph from the wall clock (animates via Live refresh)."""
    return _SPINNER_FRAMES[int(time.monotonic() * 10) % len(_SPINNER_FRAMES)]


def _fmt_duration(seconds: float | None) -> str:
    """Human-readable elapsed time, e.g. ``1.2s`` or ``3m04s``."""
    if seconds is None:
        return "—"
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), _SECONDS_PER_MINUTE)
    return f"{minutes}m{secs:02d}s"


def _fmt_resources(task: BaseTask) -> str:
    """Compact ``cpus/gpus/mem/walltime`` summary, blank if unspecified."""
    res = task.resources
    if res is None:
        return ""

    parts: list[str] = []
    if res.cpus is not None:
        parts.append(f"{res.cpus}cpu")
    if res.gpus is not None:
        parts.append(f"{res.gpus}gpu")
    if res.memory_gb is not None:
        parts.append(f"{res.memory_gb}G")
    if res.walltime is not None:
        parts.append(str(res.walltime))

    return " ".join(parts)


def _fmt_target(task: BaseTask) -> str:
    """``kind`` of the task's target (plus location when cheaply available)."""
    target = task.target
    if target is None:
        return ""

    kind = target.kind
    try:
        location = target.location_id
    except Exception:
        return kind

    if location and location not in (kind, ""):
        return f"{kind}·{location}"

    return kind


class _DashboardView:
    """
    Thin renderable so Rich ``Live`` recomputes the dashboard every frame.
    """

    def __init__(self, subscriber: "WorkflowTUISubscriber") -> None:
        self._subscriber = subscriber

    def __rich__(self) -> RenderableType:
        return self._subscriber.render()


class WorkflowTUISubscriber(BaseEventSubscriber):
    """
    Render a live dashboard of a running workflow from the event bus.
    """

    # Opt-in only: never auto-registered, so bus.start() won't construct it.
    add_to_registry: ClassVar[bool] = False
    subscriber_type: str = "workflow_tui"
    # Subscribe to every event.
    events: ClassVar[EventFilterType] = (BaseEvent,)

    _console: Console = PrivateAttr(default_factory=_make_console)
    _live: Live | None = PrivateAttr(default=None)
    _workflow: BaseWorkflow | None = PrivateAttr(default=None)

    # Execution scope (planned task ids) for an honest progress total.
    _scope: set[str] = PrivateAttr(default_factory=set)
    _started_at: float | None = PrivateAttr(default=None)

    # Per-task timing, derived from status transitions / completion events.
    _start: dict[str, float] = PrivateAttr(default_factory=dict)
    _elapsed: dict[str, float] = PrivateAttr(default_factory=dict)

    _log: deque[_LogLine] = PrivateAttr(
        default_factory=lambda: deque(maxlen=500)
    )
    _last_transfer: tuple[str, float] | None = PrivateAttr(default=None)
    _error: tuple[str, str] | None = PrivateAttr(default=None)
    _paused: bool = PrivateAttr(default=False)

    def setup(self) -> None:
        """No startup work needed."""

    def track(
        self, workflow: BaseWorkflow, trigger_id: str | None = None
    ) -> None:
        """
        Render *workflow*'s tasks before the run starts.
        """
        self._workflow = workflow
        try:
            target = trigger_id or (
                workflow.tasks[0].id if workflow.tasks else None
            )
            if target is not None:
                self._scope = set(
                    execution_plan(
                        workflow.tasks, trigger_id=target, edges=workflow.edges
                    )
                )
        except Exception:
            self._scope = {t.id for t in workflow.tasks}

    @contextmanager
    def live(self) -> Iterator[None]:
        """Drive the Rich ``Live`` dashboard for the ``with`` body.

        Redirects loguru's terminal sink into the log pane for the duration so
        log lines never corrupt the display, restoring it on exit.
        """
        horus_logger.redirect_terminal(self._log_sink)
        self._started_at = time.monotonic()
        view = _DashboardView(self)
        try:
            with Live(
                view,
                console=self._console,
                refresh_per_second=8,
                transient=False,
            ) as live:
                self._live = live
                try:
                    yield
                except BaseException as exc:
                    self._capture_error(exc)
                    raise
                finally:
                    self._live = None
                    # Final repaint so terminating statuses (and any error
                    # panel) are shown before Live tears down.
                    live.update(view)
        finally:
            horus_logger.restore_terminal()

    def handle(self, event: BaseEvent) -> None:
        """React to bus events: pause for interactions, feed the log pane."""
        # If the interaction is asked, pause the live dashboard so the
        # prompt is clean.
        if isinstance(event, InteractionAskedEvent):
            self._pause()
            return

        # If the interaction is answered or failed, resume the live dashboard.
        if isinstance(
            event, (InteractionAnsweredEvent, InteractionFailedEvent)
        ):
            self._resume()

        self._note_timings()
        self._record_log(event)

        if isinstance(event, ArtifactEvent):
            self._last_transfer = (event.artifact_id, time.monotonic())

    def _pause(self) -> None:
        """Stop the Live so an interaction prompt has a clean terminal."""
        if self._live is not None and not self._paused:
            self._live.stop()
            self._paused = True

    def _resume(self) -> None:
        """Restart the Live after an interaction completes."""
        if self._live is not None and self._paused:
            self._live.start(refresh=True)
            self._paused = False

    def _note_timings(self) -> None:
        """Record per-task start/elapsed from the live workflow statuses."""
        workflow = self._workflow
        if workflow is None:
            return
        now = time.monotonic()
        for task in workflow.tasks:
            if task.status is TaskStatus.RUNNING:
                self._start.setdefault(task.id, now)
            elif (
                task.status in _TERMINAL
                and task.id in self._start
                and task.id not in self._elapsed
            ):
                self._elapsed[task.id] = now - self._start[task.id]

    def _task_elapsed(self, task: "BaseTask") -> float | None:
        """Elapsed seconds for *task*: live for RUNNING, frozen once done."""
        if task.id in self._elapsed:
            return self._elapsed[task.id]
        if task.status is TaskStatus.RUNNING:
            start = self._start.get(task.id)
            return None if start is None else time.monotonic() - start
        return None

    def _record_log(self, event: BaseEvent) -> None:
        """Append a curated notification line for an event.

        Events are the single source for these lines; the loguru sink skips the
        ``LogsSubscriber`` echoes (see :meth:`_log_sink`) so each shows once.
        """
        if isinstance(event, HorusTaskEvent):
            icon, style = "▶", "white"
        elif isinstance(event, ArtifactEvent):
            icon, style = "⇅", "cyan"
        elif isinstance(event, HorusWorkflowEvent):
            icon, style = "◆", "magenta"
        else:
            icon, style = "·", _LEVEL_STYLE.get(event.level, "white")
        if not event.message:
            return  # nothing useful to show for an empty-message event
        self._log.append(
            _LogLine(time.time(), style, icon, str(event.message))
        )

    def _log_sink(self, message: "Message") -> None:
        """
        Loguru sink: push genuine log records into the pane (not stdout).
        """
        record = message.record
        name = record["name"] or ""
        if name.endswith("event.log_subscriber"):
            return
        style = _LEVEL_STYLE.get(record["level"].name, "white")
        self._log.append(
            _LogLine(record["time"].timestamp(), style, "•", record["message"])
        )

    def _capture_error(self, exc: BaseException) -> None:
        """Remember the failed task + error for the failure panel."""
        name = exc.__class__.__name__
        if self._workflow is not None:
            for task in self._workflow.tasks:
                if task.status is TaskStatus.FAILED:
                    name = task.name
                    break
        self._error = (name, str(exc) or exc.__class__.__name__)

    def render(self) -> RenderableType:
        """Compose the full dashboard."""
        workflow = self._workflow
        if workflow is None:
            try:
                workflow = HorusContext.get_context().workflow
            except Exception:  # no booted context → nothing to show
                workflow = None

        if workflow is None:
            return Panel(Text(_("No active workflow."), style="dim"))

        sections: list[RenderableType] = [
            self._render_header(workflow),
            self._render_progress(workflow),
            Columns(
                [self._render_table(workflow), self._render_tree(workflow)],
                expand=True,
            ),
            self._render_log(),
        ]
        if self._error is not None:
            sections.append(self._render_error())
        return Group(*sections)

    def _render_header(self, workflow: BaseWorkflow) -> RenderableType:
        status = workflow.status
        style = _WF_STATUS_STYLE.get(status, "white")
        elapsed = (
            None
            if self._started_at is None
            else time.monotonic() - self._started_at
        )
        line = Text.assemble(
            (workflow.name, "bold"),
            ("  ·  ", "dim"),
            (status.value.upper(), style),
            ("  ·  ", "dim"),
            (_("elapsed ") + _fmt_duration(elapsed), "dim"),
        )
        if self._last_transfer is not None:
            art_id, when = self._last_transfer
            if time.monotonic() - when < _TRANSFER_LINGER_S:
                line.append("\n")
                line.append(
                    _("⇅ transferring artifact %(id)s") % {"id": art_id},
                    style="cyan",
                )
        return Panel(line, border_style=style, padding=(0, 1))

    def _render_progress(self, workflow: BaseWorkflow) -> RenderableType:
        scope = self._scope or {t.id for t in workflow.tasks}
        total = len(scope)
        tasks = [t for t in workflow.tasks if t.id in scope]
        done = sum(1 for t in tasks if t.status in _TERMINAL)
        failed = any(t.status is TaskStatus.FAILED for t in tasks)
        bar = ProgressBar(
            total=max(total, 1),
            completed=done,
            width=40,
            complete_style="red" if failed else "green",
            finished_style="red" if failed else "green",
        )
        label = Text(f"  {done}/{total} " + _("tasks"), style="bold")
        grid = Table.grid(padding=(0, 1))
        grid.add_row(bar, label)
        return grid

    def _render_table(self, workflow: BaseWorkflow) -> RenderableType:
        table = Table(
            title=_("Tasks"),
            title_style="bold",
            expand=True,
            header_style="dim",
        )
        table.add_column("", no_wrap=True, width=2)
        table.add_column(_("Task"), no_wrap=True)
        table.add_column(_("Target"), style="dim")
        table.add_column(_("Resources"), style="dim")
        table.add_column(_("Elapsed"), justify="right")
        table.add_column(_("Runs"), justify="right", style="dim")
        for task in workflow.tasks:
            style = _STATUS_STYLE.get(task.status, "white")
            if task.status is TaskStatus.RUNNING:
                glyph = Text(_spinner_frame(), style=style)
            else:
                glyph = Text(_STATUS_GLYPH.get(task.status, "?"), style=style)
            table.add_row(
                glyph,
                Text(task.name, style=style),
                _fmt_target(task),
                _fmt_resources(task),
                _fmt_duration(self._task_elapsed(task)),
                str(getattr(task, "runs", "")),
            )
        return table

    def _render_tree(self, workflow: BaseWorkflow) -> RenderableType:
        """Dependency DAG, nodes colored by current status."""
        deps = build_dependencies(workflow.tasks, workflow.edges)
        names = {t.id: t.name for t in workflow.tasks}
        status = {t.id: t.status for t in workflow.tasks}
        # Children of each task (inverse of dependencies) + the roots.
        children: dict[str, list[str]] = {tid: [] for tid in deps}
        for tid, upstream in deps.items():
            for up in upstream:
                children[up].append(tid)
        roots = [tid for tid, up in deps.items() if not up]

        tree = Tree(_("Dependencies"), style="bold")

        def add(node: Tree, tid: str, seen: set[str]) -> None:
            if tid in seen:  # guard against cycles in display
                return
            seen.add(tid)
            style = _STATUS_STYLE.get(
                status.get(tid, TaskStatus.IDLE), "white"
            )
            branch = node.add(Text(names.get(tid, tid), style=style))
            for child in children.get(tid, []):
                add(branch, child, seen)

        for root in roots:
            add(tree, root, set())
        return (
            Panel(tree, padding=(0, 1))
            if roots
            else Panel(Text(_("no dependencies"), style="dim"))
        )

    def _render_log(self) -> RenderableType:
        lines = list(self._log)[-_LOG_LINES:]
        if not lines:
            body: RenderableType = Text(_("waiting for events…"), style="dim")
        else:
            text = Text()
            for i, entry in enumerate(lines):
                if i:
                    text.append("\n")
                stamp = time.strftime("%H:%M:%S", time.localtime(entry.when))
                text.append(f"{stamp} ", style="dim")
                text.append(f"{entry.icon} ", style=entry.style)
                text.append(entry.text, style=entry.style)
            body = text
        return Panel(body, title=_("Log"), title_align="left", padding=(0, 1))

    def _render_error(self) -> RenderableType:
        assert self._error is not None
        name, message = self._error
        body = Text.assemble(
            (name + "\n", "bold red"),
            (message, "red"),
        )
        return Panel(
            body, title=_("Failed"), border_style="red", padding=(0, 1)
        )


def render_workflow(workflow: BaseWorkflow, trigger_id: str) -> None:
    """
    Run the body under the live dashboard, the same one ``horus run`` uses.

    Use this to drive a Python-defined workflow (one that can't be expressed in
    YAML, e.g. ``FunctionTask`` interactions) with the TUI. The runtime must
    already be booted::

        from horus_builtin import render_workflow

        ctx = HorusContext.boot()
        render_workflow(wf, trigger_id="first")
    """
    ctx = HorusContext.get_context()
    tui = WorkflowTUISubscriber()
    tui.setup()
    tui.track(workflow, trigger_id=trigger_id)
    ctx.bus.subscribe(tui)
    with tui.live():
        asyncio.run(workflow.run(trigger_id=trigger_id))
