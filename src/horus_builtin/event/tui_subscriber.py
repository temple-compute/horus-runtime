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
live Textual dashboard while a workflow runs: a header (workflow status + wall
clock), a task progress bar, a per-task table beside the dependency DAG, a
scrolling log/event pane, and a failure panel.

Fan-out aggregation
-------------------
A ``map:``/``loop:``/``sub:`` expander derives one clone task per item at run
time (dozens, hundreds, or thousands of them). Rendering every clone as its
own table row and DAG node overflows the terminal, so the dashboard collapses
each expander into a **single** row/node showing ``done/total`` fan-out state
(see :attr:`BaseTask.expanded_from`). Press ``c`` to toggle the full per-clone
rows; the DAG tree keeps each clone as a collapsed child of its expander so a
group can still be expanded to inspect individual clones. Non-terminal clones
are surfaced in the failure panel.
"""

import asyncio
import sys
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

from pydantic import PrivateAttr
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Log, Static, Tree
from textual.widgets._tree import TreeNode

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
    from loguru import Message as LoguruMessage


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

# Priority for collapsing a fan-out group to one status: failures first, then
# live work, then queued/terminal states.
_STATUS_PRIORITY: dict[TaskStatus, int] = {
    TaskStatus.FAILED: 6,
    TaskStatus.RUNNING: 5,
    TaskStatus.PENDING: 4,
    TaskStatus.COMPLETED: 3,
    TaskStatus.CANCELED: 2,
    TaskStatus.SKIPPED: 1,
    TaskStatus.IDLE: 0,
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
assert _STATUS_PRIORITY.keys() == set(TaskStatus), (
    "TUI - missing priorities for: "
    f"{set(TaskStatus) - _STATUS_PRIORITY.keys()}"
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

# How many failed clones the failure panel lists before truncating.
_FAILURE_SHOWN = 10

# Live refresh rate. The spinner advances at this same rate so its animation
# can't beat against the frame rate.
_REFRESH_HZ = 8

# How often (seconds) the DAG tree refreshes node/leaf labels without a
# structural change. The tree is only *rebuilt* on structure changes (new
# clones appearing); label-only refreshes are throttled to this cadence.
_TREE_REFRESH_S = 0.5


class _LogLine(NamedTuple):
    """One rendered entry in the log/event pane."""

    when: float  # epoch seconds, for HH:MM:SS formatting
    style: str
    icon: str
    text: str


# DataTable column keys, used for targeted cell updates.
_COL_GLYPH = "glyph"
_COL_NAME = "name"
_COL_CLONES = "clones"
_COL_TARGET = "target"
_COL_RESOURCES = "resources"
_COL_ELAPSED = "elapsed"
_COL_RUNS = "runs"


@dataclass(frozen=True)
class _TaskRow:
    """One row of the Tasks table, pre-aggregated for the view."""

    key: str  # task id (expander id for an aggregate row)
    glyph: str
    name: str
    clones: str  # ``done/total`` fan-out column, empty for plain tasks
    target: str
    resources: str
    elapsed: str
    runs: str
    style: str


@dataclass(frozen=True)
class _TreeNode:
    """One node of the collapsed dependency DAG."""

    key: str  # task id, or expander id when collapsed
    label: str
    style: str
    clones: list[tuple[str, str, str]]  # (clone name, style, id)
    children: list["_TreeNode"]


class _TUIEvent(Message):
    """Refresh the dashboard after a bus event."""


class _TUIFinished(Message):
    """The workflow run completed; show the final state."""


class _TUISuspend(Message):
    """An interaction asked for the terminal; suspend the app."""


class _TUIResume(Message):
    """The interaction resolved; resume the app."""


def _make_console() -> Console:
    """
    Console bound to the *real* stdout.

    Binding to ``sys.__stdout__`` keeps ``is_terminal`` stable and writes
    frames to the real terminal.
    """
    return Console(file=sys.__stdout__ or sys.stdout)


def _spinner_frame() -> str:
    """Pick a spinner glyph from the wall clock (animates via refresh)."""
    tick = int(time.monotonic() * _REFRESH_HZ)
    return _SPINNER_FRAMES[tick % len(_SPINNER_FRAMES)]


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


def _aggregate_status(statuses: list[TaskStatus]) -> TaskStatus:
    """Worst status among *statuses*, by :data:`_STATUS_PRIORITY`."""
    if not statuses:
        return TaskStatus.IDLE
    return max(statuses, key=lambda s: _STATUS_PRIORITY.get(s, 0))


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
    _app: "_WorkflowTUIApp | None" = PrivateAttr(default=None)
    _workflow: BaseWorkflow | None = PrivateAttr(default=None)

    # Task the run was triggered from; the execution scope behind the
    # progress total is re-planned from it on every frame (see _scope_ids).
    _trigger: str | None = PrivateAttr(default=None)
    _started_at: float | None = PrivateAttr(default=None)

    # Per-task timing, derived from status transitions / completion events.
    _start: dict[str, float] = PrivateAttr(default_factory=dict)
    _elapsed: dict[str, float] = PrivateAttr(default_factory=dict)

    _log: deque[_LogLine] = PrivateAttr(
        default_factory=lambda: deque(maxlen=500)
    )
    _last_transfer: tuple[str, float] | None = PrivateAttr(default=None)
    _error: tuple[str, str] | None = PrivateAttr(default=None)
    _finished: bool = PrivateAttr(default=False)
    _finished_at: float | None = PrivateAttr(default=None)

    def setup(self) -> None:
        """No startup work needed."""

    def track(
        self, workflow: BaseWorkflow, trigger_id: str | None = None
    ) -> None:
        """
        Render *workflow*'s tasks before the run starts.
        """
        self._workflow = workflow
        self._trigger = trigger_id or (
            workflow.tasks[0].id if workflow.tasks else None
        )

    @property
    def workflow(self) -> BaseWorkflow | None:
        """The workflow being tracked, if any."""
        return self._workflow

    @property
    def log(self) -> deque[_LogLine]:
        """The shared log/event pane buffer."""
        return self._log

    @property
    def error(self) -> tuple[str, str] | None:
        """``(name, message)`` of the last workflow-level error, if any."""
        return self._error

    def _scope_ids(self, workflow: BaseWorkflow) -> set[str]:
        """
        Ids in the run's execution scope, re-planned from *workflow*'s
        current tasks and edges.

        A ``map:`` or ``sub:`` expander adds its clones, and the edges that
        reach the gather task, only when it runs (see
        :meth:`~horus_runtime.core.workflow.base.BaseWorkflow.expand`), so a
        plan frozen before the run undercounts every fan-out: a five-clone
        loop map reported ``1/1 tasks``. Re-planning per frame counts the
        DAG as it actually is.
        """
        if self._trigger is None:
            return {t.id for t in workflow.tasks}
        try:
            return set(
                execution_plan(
                    workflow.tasks,
                    trigger_id=self._trigger,
                    edges=workflow.edges,
                )
            )
        except Exception:
            return {t.id for t in workflow.tasks}

    def handle(self, event: BaseEvent) -> None:
        """React to bus events: pause for interactions, feed the log pane."""
        # If the interaction is asked, suspend the app so the prompt has a
        # clean terminal (the interaction transport reads stdin directly).
        if isinstance(event, InteractionAskedEvent):
            self._post(_TUISuspend())
            return

        # If the interaction is answered or failed, resume the app.
        if isinstance(
            event, (InteractionAnsweredEvent, InteractionFailedEvent)
        ):
            self._post(_TUIResume())

        self._note_timings()
        self._record_log(event)

        if isinstance(event, ArtifactEvent):
            self._last_transfer = (event.artifact_id, time.monotonic())

        self._post(_TUIEvent())

    def _post(self, message: Message) -> None:
        """Queue *message* for the app when one is running."""
        app = self._app
        if app is not None:
            app.post_message(message)

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

    def _task_elapsed(self, task: BaseTask) -> float | None:
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

    def _log_sink(self, message: "LoguruMessage") -> None:
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

    def _fanout_groups(self) -> dict[str, list[BaseTask]]:
        """Clone tasks grouped by their expander's id (``expanded_from``)."""
        workflow = self._workflow
        if workflow is None:
            return {}
        groups: dict[str, list[BaseTask]] = {}
        for task in workflow.tasks:
            if task.expanded_from is not None:
                groups.setdefault(task.expanded_from, []).append(task)
        return groups

    def task_rows(self, *, show_clones: bool = False) -> list[_TaskRow]:
        """
        Table rows with every fan-out collapsed into one aggregate row.

        With *show_clones*, the clones of each expander are emitted right
        after their aggregate row (the ``c`` binding).
        """
        workflow = self._workflow
        if workflow is None:
            return []
        groups = self._fanout_groups()
        seen: set[str] = set()
        rows: list[_TaskRow] = []
        for task in workflow.tasks:
            if task.id in seen:
                continue
            if task.expanded_from is not None:
                continue  # folded into its expander's row (below if shown)
            clones = groups.get(task.id)
            if clones:
                seen.update(clone.id for clone in clones)
                rows.append(self._aggregate_row(task, clones))
                if show_clones:
                    rows.extend(self._single_row(clone) for clone in clones)
            else:
                rows.append(self._single_row(task))
        return rows

    def _aggregate_row(
        self, task: BaseTask, clones: list[BaseTask]
    ) -> _TaskRow:
        """One table row summarizing an expander plus its clones."""
        members = [task, *clones]
        worst = _aggregate_status([t.status for t in members])
        style = _STATUS_STYLE.get(worst, "white")
        glyph = (
            _spinner_frame()
            if worst is TaskStatus.RUNNING
            else _STATUS_GLYPH.get(worst, "?")
        )
        done = sum(1 for t in members if t.status in _TERMINAL)
        elapsed = sum(self._task_elapsed(t) or 0 for t in members)
        runs = sum(getattr(t, "runs", 0) for t in members)
        target = _fmt_target(task) or _fmt_target(clones[0])
        resources = _fmt_resources(task) or _fmt_resources(clones[0])
        return _TaskRow(
            key=task.id,
            glyph=glyph,
            name=task.name,
            clones=f"{done}/{len(clones)}",
            target=target,
            resources=resources,
            elapsed=_fmt_duration(elapsed if elapsed else None),
            runs=str(runs),
            style=style,
        )

    def _single_row(self, task: BaseTask) -> _TaskRow:
        """One table row for a plain (non-expanded) task or a shown clone."""
        style = _STATUS_STYLE.get(task.status, "white")
        glyph = (
            _spinner_frame()
            if task.status is TaskStatus.RUNNING
            else _STATUS_GLYPH.get(task.status, "?")
        )
        return _TaskRow(
            key=task.id,
            glyph=glyph,
            name=task.name,
            clones="",
            target=_fmt_target(task),
            resources=_fmt_resources(task),
            elapsed=_fmt_duration(self._task_elapsed(task)),
            runs=str(getattr(task, "runs", "")),
            style=style,
        )

    def tree_nodes(self) -> list[_TreeNode]:
        """
        Collapsed dependency DAG: expander nodes replace their clones.

        Each expander node carries its clones as leaf entries so the tree can
        offer per-clone drill-down while staying compact at any fan-out scale.
        """
        workflow = self._workflow
        if workflow is None:
            return []
        deps = build_dependencies(workflow.tasks, workflow.edges)

        parent: dict[str, str] = {}
        for task in workflow.tasks:
            if task.expanded_from is not None:
                parent[task.id] = task.expanded_from

        def key(tid: str) -> str:
            return parent.get(tid, tid)

        merged: dict[str, set[str]] = {}
        for tid, upstream in deps.items():
            bucket = merged.setdefault(key(tid), set())
            for up in upstream:
                if key(up) != key(tid):
                    bucket.add(key(up))

        children: dict[str, set[str]] = {k: set() for k in merged}
        for tid, upstream in merged.items():
            for up in upstream:
                children[up].add(tid)

        clones_by_parent: dict[str, list[BaseTask]] = {}
        for task in workflow.tasks:
            if task.expanded_from is not None:
                clones_by_parent.setdefault(task.expanded_from, []).append(
                    task
                )

        names = {t.id: t.name for t in workflow.tasks}
        statuses = {t.id: t.status for t in workflow.tasks}
        roots = sorted(k for k, upstream in merged.items() if not upstream)

        def build(tid: str, seen: set[str]) -> _TreeNode:
            seen.add(tid)
            clones = clones_by_parent.get(tid, [])
            if clones:
                worst = _aggregate_status([t.status for t in clones])
                style = _STATUS_STYLE.get(worst, "white")
                done = sum(1 for t in clones if t.status in _TERMINAL)
                label = (
                    f"{names.get(tid, tid)}  \u00d7{len(clones)}  "
                    f"{done}/{len(clones)}"
                )
                leaves = [
                    (t.name, _STATUS_STYLE.get(t.status, "white"), t.id)
                    for t in clones
                ]
            else:
                style = _STATUS_STYLE.get(
                    statuses.get(tid, TaskStatus.IDLE), "white"
                )
                label = names.get(tid, tid)
                leaves = []
            kids = [
                build(child, seen)
                for child in sorted(children.get(tid, ()))
                if child not in seen
            ]
            return _TreeNode(
                key=tid, label=label, style=style, clones=leaves, children=kids
            )

        return [build(root, set()) for root in roots]

    def failure_rows(self) -> list[str]:
        """Names of failed tasks/clones, bounded for the panel."""
        workflow = self._workflow
        if workflow is None:
            return []
        failed = [t for t in workflow.tasks if t.status is TaskStatus.FAILED]
        lines = [t.name for t in failed[:_FAILURE_SHOWN]]
        if len(failed) > _FAILURE_SHOWN:
            lines.append(
                _("... and %(n)d more") % {"n": len(failed) - _FAILURE_SHOWN}
            )
        return lines

    def _progress(self) -> tuple[int, int, bool]:
        """``(done, total, failed)`` for the scope's progress bar."""
        workflow = self._workflow
        if workflow is None:
            return 0, 1, False
        scope = self._scope_ids(workflow)
        tasks = [t for t in workflow.tasks if t.id in scope]
        done = sum(1 for t in tasks if t.status in _TERMINAL)
        failed = any(t.status is TaskStatus.FAILED for t in tasks)
        return done, max(len(scope), 1), failed

    def header_text(self) -> Text:
        """Workflow name · status · elapsed, plus transient transfer line."""
        workflow = self._workflow
        if workflow is None:
            return Text(_("No active workflow."), style="dim")
        status = workflow.status
        style = _WF_STATUS_STYLE.get(status, "white")
        elapsed = (
            None
            if self._started_at is None
            else (self._finished_at or time.monotonic()) - self._started_at
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
        if self._finished:
            line.append("\n")
            line.append(_("Workflow finished, press q to close"), style="dim")
        return line

    def progress_renderable(self) -> RenderableType:
        """Rich progress bar + ``done/total tasks`` label."""
        done, total, failed = self._progress()
        bar = ProgressBar(
            total=total,
            completed=done,
            width=40,
            complete_style="red" if failed else "green",
            finished_style="red" if failed else "green",
        )
        label = Text(f"  {done}/{total} " + _("tasks"), style="bold")
        grid = Table.grid(padding=(0, 1))
        grid.add_row(bar, label)
        return grid

    def _render_summary(self) -> RenderableType:
        """One-line receipt of the final state, for normal scrollback.

        The dashboard lives on the alternate screen and disappears when the
        app exits, so this is all the user is left with after a run.
        """
        workflow = self._workflow
        if workflow is None:
            return Text(_("No workflow ran."), style="dim")

        scope = self._scope_ids(workflow)
        tasks = [t for t in workflow.tasks if t.id in scope]
        done = sum(1 for t in tasks if t.status in _TERMINAL)
        elapsed = (
            None
            if self._started_at is None
            else time.monotonic() - self._started_at
        )
        line = Text.assemble(
            (workflow.name, "bold"),
            ("  ·  ", "dim"),
            (
                workflow.status.value.upper(),
                _WF_STATUS_STYLE.get(workflow.status, "white"),
            ),
            ("  ·  ", "dim"),
            (f"{done}/{len(tasks)} " + _("tasks"), "dim"),
            ("  ·  ", "dim"),
            (_fmt_duration(elapsed), "dim"),
        )
        if self._error is None:
            return line
        return Group(line, self._render_error())

    def _render_error(self) -> RenderableType:
        """Rich body for the failure panel's workflow-level error."""
        assert self._error is not None
        name, message = self._error
        body = Text.assemble(
            (name + "\n", "bold red"),
            (message, "red"),
        )
        return Panel(
            body, title=_("Failed"), border_style="red", padding=(0, 1)
        )

    async def _drive(self) -> None:
        """
        Run the Textual app and the workflow concurrently on this event loop.

        Ends when the user quits the app (the workflow is then cancelled) or
        when the workflow finishes first (the app stays open so the final
        state can be inspected, until the user presses ``q``).
        """
        app = _WorkflowTUIApp(self)
        self._app = app

        async def _run_workflow() -> None:
            assert self._workflow is not None
            assert self._trigger is not None
            await self._workflow.run(trigger_id=self._trigger)

        wf_task = asyncio.create_task(_run_workflow())
        app_task = asyncio.create_task(app.run_async())
        outcome: BaseException | None = None
        try:
            done, _pending = await asyncio.wait(
                {wf_task, app_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if wf_task in done:
                exc = wf_task.exception()
                if exc is not None:
                    outcome = exc
                    self._capture_error(exc)
                self._finished = True
                self._finished_at = time.monotonic()
                app.post_message(_TUIFinished())
                await app_task
            else:
                # The user quit the app first: cancel the still-running run.
                wf_task.cancel()
                with suppress(asyncio.CancelledError):
                    await wf_task
                app_exc = app_task.exception()
                if app_exc is not None:
                    raise app_exc
        finally:
            for task in (wf_task, app_task):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        if outcome is not None:
            raise outcome

    def run_dashboard(self) -> None:
        """Run the dashboard, falling back to a plain run on non-TTY output."""
        if not _is_interactive():
            assert self._workflow is not None
            assert self._trigger is not None
            asyncio.run(self._workflow.run(trigger_id=self._trigger))
            return

        # Log lines should only ever land in the dashboard's own log pane,
        # never on the real terminal (the file sink still captures everything).
        horus_logger.redirect_terminal(self._log_sink)
        self._started_at = time.monotonic()
        try:
            asyncio.run(self._drive())
        finally:
            horus_logger.restore_terminal()
            # The alternate screen is gone by now, so leave a receipt of the
            # final state behind in normal scrollback. Runs on the failure
            # path too, so a crash still reports what happened.
            self._console.print(self._render_summary())


_TEMPLE_THEME = Theme(
    name="temple",
    primary="#d4a574",  # amber accent
    secondary="#e6c8a6",  # amber-bright
    accent="#e6c8a6",
    warning="#e6b370",
    error="#e06666",
    success="#8cb99a",
    foreground="#f3f2ef",  # ink
    background="#0a0b0d",  # base
    surface="#101216",
    panel="#16181d",
    boost="#000000",
    dark=True,
    variables={
        "base": "#0a0b0d",
        "surface": "#101216",
        "raised": "#16181d",
        "ink": "#f3f2ef",
        "ink-soft": "#b4b6ba",
        "ink-muted": "#74777d",
        "line": "#23262c",
        "line-soft": "#191c21",
        "accent": "#d4a574",
        "accent-bright": "#e6c8a6",
    },
)


class _WorkflowTUIApp(App[None]):
    """Textual dashboard: scrollable, keyboard-driven, fan-out aware."""

    TITLE = "Horus"
    SUB_TITLE = _("workflow dashboard")

    BINDINGS: ClassVar[
        list[Binding | tuple[str, str] | tuple[str, str, str]]
    ] = [
        Binding("q", "quit", "Quit"),
        Binding("c", "toggle_clones", "Toggle clones"),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }
    #header {
        height: auto;
        padding: 0 1;
        border: solid $primary;
    }
    #progress {
        height: auto;
        padding: 0 1;
    }
    #middle {
        height: 1fr;
    }
    #tasks {
        width: 55%;
        height: 100%;
        border: solid $primary;
    }
    #deps {
        width: 45%;
        height: 100%;
        border: solid $primary;
    }
    #log {
        height: 8;
        border: solid $primary;
    }
    #failures {
        height: auto;
        max-height: 10;
        border: solid $error;
        display: none;
    }
    """

    def __init__(self, model: WorkflowTUISubscriber) -> None:
        super().__init__()
        self._model = model
        self._show_clones = False
        self._suspend_event: asyncio.Event | None = None
        self._suspend_task: asyncio.Task[None] | None = None

        # Cached widget references, set in on_mount.
        self._header: Static
        self._progress: Static
        self._tasks: DataTable[Any]
        self._deps: Tree[Any]
        self._log_widget: Log
        self._failures: Static

        # Last-rendered table rows by row key, for targeted cell updates.
        self._rendered: dict[
            str, tuple[str, str, str, str, str, str, str, str]
        ] = {}
        # Tree bookkeeping: structural key set + live node/leaf references.
        self._tree_structure: frozenset[str] = frozenset()
        self._tree_nodes: dict[str, TreeNode[Any]] = {}
        self._tree_leaves: dict[str, TreeNode[Any]] = {}
        self._last_tree_refresh = 0.0

    def compose(self) -> ComposeResult:
        """Lay out the dashboard's panels."""
        yield Static("", id="header")
        yield Static("", id="progress")
        tasks: DataTable[Any] = DataTable(id="tasks")
        tasks.border_title = _(" Tasks ")
        deps: Tree[Any] = Tree(_("Dependencies"), id="deps")
        deps.border_title = _(" Dependencies ")
        with Horizontal(id="middle"):
            yield tasks
            yield deps
        log = Log(max_lines=_LOG_LINES, id="log")
        log.border_title = _(" Log ")
        yield log
        failures = Static("", id="failures")
        failures.border_title = _(" Failed ")
        yield failures
        yield Footer()

    def on_mount(self) -> None:
        """Apply the Temple palette, cache widgets, render, start the timer."""
        self.register_theme(_TEMPLE_THEME)
        self.theme = "temple"
        self._header = self.query_one("#header", Static)
        self._progress = self.query_one("#progress", Static)
        self._tasks = self.query_one("#tasks", DataTable)
        self._deps = self.query_one("#deps", Tree)
        self._log_widget = self.query_one("#log", Log)
        self._failures = self.query_one("#failures", Static)
        self._deps.show_root = False
        self._tasks.add_columns(
            ("", _COL_GLYPH),
            (_("Task"), _COL_NAME),
            (_("Clones"), _COL_CLONES),
            (_("Target"), _COL_TARGET),
            (_("Resources"), _COL_RESOURCES),
            (_("Elapsed"), _COL_ELAPSED),
            (_("Runs"), _COL_RUNS),
        )
        self._refresh()
        self.set_interval(1 / _REFRESH_HZ, self._refresh)

    async def action_quit(self) -> None:
        """Quit the dashboard (the workflow keeps its run state)."""
        self.exit()

    def action_toggle_clones(self) -> None:
        """Toggle the Tasks table between aggregate and full clone rows."""
        self._show_clones = not self._show_clones
        self._refresh()

    def on_tui_event(self, _message: _TUIEvent) -> None:
        """A bus event arrived: repaint."""
        self._refresh()

    def on_tui_finished(self, _message: _TUIFinished) -> None:
        """The workflow run ended: show the final state."""
        self._refresh()

    def on_tui_suspend(self, _message: _TUISuspend) -> None:
        """An interaction asked for the terminal: free it for the prompt."""
        if self._suspend_event is None:
            self._suspend_event = asyncio.Event()
            self._suspend_task = asyncio.create_task(
                self._suspended_until_resumed()
            )

    async def _suspended_until_resumed(self) -> None:
        """Suspend the app until the pending interaction is resolved."""
        assert self._suspend_event is not None
        try:
            with self.suspend():
                await self._suspend_event.wait()
        finally:
            self._suspend_event = None
            self._suspend_task = None

    def on_tui_resume(self, _message: _TUIResume) -> None:
        """The pending interaction resolved: resume the app."""
        if self._suspend_event is not None:
            self._suspend_event.set()

    def _refresh(self) -> None:
        """Sync header, progress, task rows, failures, log, and DAG tree.

        All paths are incremental: only changed cells/labels are touched, so
        the dashboard is cheap to repaint at the refresh rate and on events.
        """
        model = self._model
        if model.workflow is None:
            return
        self._header.update(model.header_text())
        self._progress.update(model.progress_renderable())
        self._sync_table()
        self._render_failures()
        self._render_log()
        self._maybe_sync_tree()

    def _sync_table(self) -> None:
        """Add/remove/update only the DataTable rows that actually changed."""
        table = self._tasks
        rows = self._model.task_rows(show_clones=self._show_clones)
        keys = [row.key for row in rows]
        key_set = set(keys)

        # Drop rows whose task is gone (e.g. a collapsed fan-out re-derived).
        for key in [k for k in self._rendered if k not in key_set]:
            table.remove_row(key)
            del self._rendered[key]

        for row in rows:
            signature = (
                row.glyph,
                row.name,
                row.clones,
                row.target,
                row.resources,
                row.elapsed,
                row.runs,
                row.style,
            )
            if row.key not in self._rendered:
                table.add_row(
                    Text(row.glyph, style=row.style),
                    Text(row.name, style=row.style),
                    row.clones,
                    row.target,
                    row.resources,
                    row.elapsed,
                    row.runs,
                    key=row.key,
                )
                self._rendered[row.key] = signature
            elif self._rendered[row.key] != signature:
                self._update_row_cells(table, row, self._rendered[row.key])
                self._rendered[row.key] = signature

    @staticmethod
    def _update_row_cells(
        table: DataTable[Any],
        row: _TaskRow,
        previous: tuple[str, str, str, str, str, str, str, str],
    ) -> None:
        """Update only the cells of *row* that changed since last render."""
        if previous[0] != row.glyph or previous[7] != row.style:
            table.update_cell(
                row.key, _COL_GLYPH, Text(row.glyph, style=row.style)
            )
        if previous[1] != row.name or previous[7] != row.style:
            table.update_cell(
                row.key, _COL_NAME, Text(row.name, style=row.style)
            )
        if previous[2] != row.clones:
            table.update_cell(row.key, _COL_CLONES, row.clones)
        if previous[3] != row.target:
            table.update_cell(row.key, _COL_TARGET, row.target)
        if previous[4] != row.resources:
            table.update_cell(row.key, _COL_RESOURCES, row.resources)
        if previous[5] != row.elapsed:
            table.update_cell(row.key, _COL_ELAPSED, row.elapsed)
        if previous[6] != row.runs:
            table.update_cell(row.key, _COL_RUNS, row.runs)

    def _render_log(self) -> None:
        """Flush newly-arrived log lines into the pane.

        Lines are consumed with ``popleft`` rather than indexed, so the pane
        never re-reads a deque index that a burst of events may have truncated
        under the buffer's ``maxlen``.
        """
        while True:
            try:
                entry = self._model.log.popleft()
            except IndexError:
                break
            stamp = time.strftime("%H:%M:%S", time.localtime(entry.when))
            self._log_widget.write_line(f"{stamp} {entry.icon} {entry.text}")

    def _render_failures(self) -> None:
        """Show the failure panel only when there is something to report."""
        lines = self._model.failure_rows()
        if self._model.error is not None:
            name, message = self._model.error
            lines = [f"{name}: {message}", *lines]
        self._failures.display = bool(lines)
        if lines:
            self._failures.update("\n".join(lines))

    def _maybe_sync_tree(self) -> None:
        """
        Rebuild the DAG tree only when its structure changes; otherwise
        refresh labels at a throttled cadence so status colors/counts stay
        live without reconstructing the widget on every event.
        """
        model = self._model
        workflow = model.workflow
        if workflow is None:
            return
        nodes = model.tree_nodes()
        new_structure = frozenset(n.key for n in nodes)
        if new_structure != self._tree_structure:
            self._tree_structure = new_structure
            self._rebuild_tree(nodes)
            return
        if time.monotonic() - self._last_tree_refresh >= _TREE_REFRESH_S:
            self._last_tree_refresh = time.monotonic()
            self._refresh_tree_labels(nodes)

    def _rebuild_tree(self, nodes: list[_TreeNode]) -> None:
        """Reconstruct the whole DAG tree and cache node/leaf references."""
        self._tree_nodes.clear()
        self._tree_leaves.clear()
        self._deps.clear()
        for node in nodes:
            self._add_tree_node(self._deps.root, node)

    def _refresh_tree_labels(self, nodes: list[_TreeNode]) -> None:
        """Update labels/styles of nodes and clone leaves that changed."""
        for node in nodes:
            rendered = self._tree_nodes.get(node.key)
            if rendered is not None and str(rendered.label) != node.label:
                rendered.set_label(Text(node.label, style=node.style))
            for clone_name, clone_style, clone_id in node.clones:
                leaf = self._tree_leaves.get(clone_id)
                if leaf is not None:
                    leaf.set_label(Text(clone_name, style=clone_style))

    def _add_tree_node(
        self,
        parent: TreeNode[Any],
        node: _TreeNode,
    ) -> None:
        """Add *node* (plus its collapsed clone leaves) to the DAG tree."""
        branch = parent.add(Text(node.label, style=node.style), data=node.key)
        self._tree_nodes[node.key] = branch
        for clone_name, clone_style, clone_id in node.clones:
            leaf = branch.add(Text(clone_name, style=clone_style))
            self._tree_leaves[clone_id] = leaf
        if node.clones:
            branch.collapse()
        for child in node.children:
            self._add_tree_node(branch, child)


def _is_interactive() -> bool:
    """A real TTY on both ends (Textual needs stdout, prompts read stdin)."""
    return bool(sys.stdin.isatty()) and bool(sys.stdout.isatty())


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
    tui.run_dashboard()
