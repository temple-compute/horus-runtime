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

import asyncio
import time
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import PrivateAttr
from rich.console import Console
from textual.widgets import DataTable, Static, Tree
from textual.widgets._tree import TreeNode

from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.event.tui_subscriber import (
    _FAILURE_SHOWN,
    WorkflowTUISubscriber,
    _aggregate_status,
    _fmt_duration,
    _fmt_resources,
    _fmt_target,
    _TUIResume,
    _TUISuspend,
    _WorkflowTUIApp,
)
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.core.interaction.transport import (
    InteractionAnsweredEvent,
    InteractionAskedEvent,
)
from horus_runtime.core.resources import ResourceRequest
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.event.subscriber import BaseEventSubscriber


def _task(tid: str, name: str | None = None, **kwargs: object) -> HorusTask:
    """A trivial local task (optionally tagged as a fan-out clone)."""
    return HorusTask(
        id=tid,
        name=name or tid,
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        **kwargs,  # type: ignore[arg-type]
    )


def _fanout_workflow(clones: int = 60) -> HorusWorkflow:
    """A three-task workflow whose middle task fans out into clones."""
    tasks: list[BaseTask] = [
        _task("gen", "Generate"),
        _task("calc", "Compute"),
    ]
    for i in range(clones):
        tasks.append(_task(f"calc[{i:02d}]", expanded_from="calc"))
    tasks.append(_task("gather", "Gather"))
    return HorusWorkflow(name="demo_wf", tasks=tasks)


def _tracked(clones: int = 60) -> WorkflowTUISubscriber:
    """A subscriber tracking :func:`_fanout_workflow`."""
    tui = WorkflowTUISubscriber()
    tui.track(_fanout_workflow(clones))
    return tui


def _clones(tui: WorkflowTUISubscriber) -> list[BaseTask]:
    """The fan-out clone tasks of *tui*'s workflow."""
    assert tui.workflow is not None
    return [t for t in tui.workflow.tasks if t.expanded_from is not None]


def _flatten(node: TreeNode[Any]) -> list[TreeNode[Any]]:
    """Flatten a Textual tree node and its descendants."""
    nodes = [node]
    for child in node.children:
        nodes.extend(_flatten(child))
    return nodes


@pytest.mark.unit
class TestWorkflowTUISubscriber:
    """Tests for ``WorkflowTUISubscriber``."""

    def test_not_auto_registered(self) -> None:
        """It must stay out of the auto-instantiated registry."""
        registered = BaseEventSubscriber.registry.values()
        assert WorkflowTUISubscriber not in registered

    def test_task_rows_collapse_fanout(self) -> None:
        """Sixty clones collapse into one ``done/total`` aggregate row."""
        tui = _tracked()
        rows = tui.task_rows()
        assert [row.name for row in rows] == ["Generate", "Compute", "Gather"]
        aggregate = next(row for row in rows if row.name == "Compute")
        assert aggregate.clones == "0/60"
        assert not any(row.name.startswith("calc[") for row in rows)

    def test_task_rows_show_clones_when_requested(self) -> None:
        """The ``c`` toggle adds one row per clone after the aggregate."""
        tui = _tracked()
        rows = tui.task_rows(show_clones=True)
        clones = [row for row in rows if row.name.startswith("calc[")]
        assert len(clones) == 60
        assert len(rows) == 63

    def test_aggregate_status_reflects_worst_clone(self) -> None:
        """A failed clone paints the whole fan-out red with a ✗ glyph."""
        tui = _tracked()
        clones = _clones(tui)
        clones[0].status = TaskStatus.FAILED
        for clone in clones[1:10]:
            clone.status = TaskStatus.COMPLETED

        aggregate = next(r for r in tui.task_rows() if r.name == "Compute")
        assert aggregate.clones == "10/60"
        assert aggregate.glyph == "✗"
        assert aggregate.style == "bold red"

        # A clean, fully-completed fan-out is green with the full count.
        for clone in clones:
            clone.status = TaskStatus.COMPLETED
        aggregate = next(r for r in tui.task_rows() if r.name == "Compute")
        assert aggregate.clones == "60/60"
        assert aggregate.style == "bold green"

    def test_tree_nodes_collapse_fanout_with_drill_down(self) -> None:
        """The DAG tree shows one expander node with clone leaves."""
        tui = _tracked()
        nodes = tui.tree_nodes()
        expander = next(node for node in nodes if node.key == "calc")
        assert "\u00d760" in expander.label
        assert len(expander.clones) == 60
        assert "calc[00]" in expander.clones[0][0]

    def test_failure_rows_list_failed_clones(self) -> None:
        """Only failed clones surface in the failure panel."""
        tui = _tracked()
        clones = _clones(tui)
        clones[0].status = TaskStatus.FAILED
        clones[3].status = TaskStatus.FAILED
        assert tui.failure_rows() == ["calc[00]", "calc[03]"]

    def test_failure_rows_are_bounded(self) -> None:
        """More failures than the panel can show are truncated with a count."""
        tui = _tracked()
        clones = _clones(tui)
        for clone in clones:
            clone.status = TaskStatus.FAILED
        rows = tui.failure_rows()
        assert len(rows) == _FAILURE_SHOWN + 1
        assert rows[-1] == "... and 50 more"

    def test_handle_without_app_is_noop(self) -> None:
        """handle() must not raise when no app is running."""
        tui = _tracked()
        tui.handle(HorusTaskEvent(task_name="Alpha", message="hello"))
        assert len(tui.log) == 1

    def test_progress_total_follows_a_growing_dag(self) -> None:
        """
        Tasks added after ``track()`` count towards the total.

        A ``map:``/``sub:`` expander only adds its clones once it runs, so a
        scope planned before the run undercounted every fan-out.
        """
        tasks: list[BaseTask] = [
            _task("a", "Alpha"),
            _task("b", "Beta"),
        ]
        wf = HorusWorkflow(name="demo_wf", tasks=tasks)
        wf.edges.append(WorkflowEdge(source="a", target="b"))
        tui = WorkflowTUISubscriber()
        tui.track(wf, trigger_id="a")
        assert tui._progress() == (0, 2, False)

        wf.tasks.append(_task("c", "Gamma"))
        wf.edges.append(WorkflowEdge(source="b", target="c"))
        assert tui._progress() == (0, 3, False)

    def test_header_text_shows_transfer_and_finished_hint(self) -> None:
        """A recent artifact transfer and the finished state both render."""
        tui = _tracked()
        tui._last_transfer = ("energies", time.monotonic())
        tui._finished = True
        text = str(tui.header_text())
        assert "energies" in text
        assert "press q to close" in text

    def test_render_summary_includes_error(self) -> None:
        """The scrollback receipt carries the workflow-level error."""
        tui = _tracked()
        tui._capture_error(RuntimeError("boom"))
        console = Console(record=True, width=80)
        console.print(tui._render_summary())
        assert "boom" in console.export_text()

    def test_capture_error_names_the_failed_task(self) -> None:
        """The failure panel names the task rather than the exception type."""
        tui = _tracked()
        _clones(tui)[0].status = TaskStatus.FAILED
        tui._capture_error(RuntimeError("boom"))
        assert tui._error == ("calc[00]", "boom")

    def test_aggregate_status_of_no_tasks_is_idle(self) -> None:
        """An empty fan-out group aggregates to IDLE."""
        assert _aggregate_status([]) == TaskStatus.IDLE

    def test_fmt_duration_renders_minutes(self) -> None:
        """Durations of a minute or more render as ``m``/``s``."""
        assert _fmt_duration(90) == "1m30s"
        assert _fmt_duration(None) == "—"

    def test_fmt_resources_lists_requests(self) -> None:
        """Specified resources render as a compact summary."""
        task = _task("res", "Res")
        task.resources = ResourceRequest(cpus=2, memory_gb=4)
        assert _fmt_resources(task) == "2cpu 0gpu 4G"

    def test_fmt_target_degrades_when_location_unknown(self) -> None:
        """A target whose location cannot be read still shows its kind."""

        class FlakyLocation(LocalTarget):
            add_to_registry: ClassVar[bool] = False
            kind: str = "local"

            @property
            def location_id(self) -> str:
                raise RuntimeError("no location")

        task = _task("flaky", "Flaky", target=FlakyLocation())
        assert _fmt_target(task) == "local"


@pytest.mark.unit
class TestWorkflowTUIApp:
    """Headless smoke tests for the Textual dashboard."""

    async def test_app_composes_and_aggregates(self) -> None:
        """The app mounts and renders one row per collapsed fan-out group."""
        app = _WorkflowTUIApp(_tracked())
        async with app.run_test(size=(120, 30)):
            table = app.query_one("#tasks", DataTable)
            assert table.row_count == 3

            tree = app.query_one("#deps", Tree)
            labels = [str(node.label) for node in _flatten(tree.root)]
            assert any("\u00d760" in label for label in labels)
            assert any(label.startswith("calc[") for label in labels)

    async def test_app_toggle_clones(self) -> None:
        """The ``c`` binding expands the table to per-clone rows."""
        app = _WorkflowTUIApp(_tracked())
        async with app.run_test(size=(120, 30)) as pilot:
            table = app.query_one("#tasks", DataTable)
            assert table.row_count == 3
            await pilot.press("c")
            assert table.row_count == 63

    async def test_interaction_suspend_and_resume(self) -> None:
        """An asked interaction suspends the app; the answer resumes it."""
        tui = _tracked()
        app = _WorkflowTUIApp(tui)
        app.on_tui_suspend(_TUISuspend())
        assert app._suspend_event is not None

        app.on_tui_resume(_TUIResume())
        await asyncio.sleep(0.01)
        assert app._suspend_event is None

    async def test_handle_posts_refresh_inside_app(self) -> None:
        """Bus events posted while the app runs repaint without error."""
        tui = _tracked()
        app = _WorkflowTUIApp(tui)
        async with app.run_test(size=(120, 30)) as pilot:
            tui._app = app
            tui.handle(HorusTaskEvent(task_name="Alpha"))
            await pilot.pause()
            asked = InteractionAskedEvent(
                interaction_kind="string",
                transport_kind="cli",
                renderer_key="cli.string",
                value_key="v",
            )
            answered = InteractionAnsweredEvent(
                interaction_kind="string", transport_kind="cli", value_key="v"
            )
            tui.handle(asked)
            await pilot.pause()
            tui.handle(answered)
            await pilot.pause()
            await asyncio.sleep(0.01)

    async def test_app_logs_events_and_shows_failures(self) -> None:
        """Bus events feed the log pane; failed clones fill the panel."""
        tui = _tracked()
        app = _WorkflowTUIApp(tui)
        async with app.run_test(size=(120, 40)):
            tui._app = app
            _clones(tui)[0].status = TaskStatus.FAILED
            tui._capture_error(RuntimeError("boom"))
            tui.handle(HorusTaskEvent(task_name="Alpha", message="hello"))
            for _ in range(20):
                await asyncio.sleep(0.01)
                failures = app.query_one("#failures", Static)
                if failures.display:
                    break
            assert failures.display is True
            assert "boom" in str(failures.render())
            assert "calc[00]" in str(failures.render())
            # The log line was drained from the shared buffer into the pane.
            assert len(tui.log) == 0


class _HangingTask(HorusTask):
    """A task that blocks forever until its run is cancelled."""

    add_to_registry: ClassVar[bool] = False
    _started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    async def _run(self) -> None:
        self._started.set()
        await asyncio.Event().wait()


@pytest.mark.unit
class TestDashboardDrive:
    """Drive-level tests: ``run_dashboard`` and the ``_drive`` loop."""

    async def test_drive_waits_for_workflow_then_user_quits(
        self,
        monkeypatch: pytest.MonkeyPatch,
        horus_context: Any,
        tmp_path: Any,
    ) -> None:
        """The app stays open until the workflow finishes, then quits."""
        del horus_context
        quit_event = asyncio.Event()

        async def _fake_run_async(
            _self: object, *_args: object, **_kwargs: object
        ) -> None:
            await quit_event.wait()

        monkeypatch.setattr(_WorkflowTUIApp, "run_async", _fake_run_async)

        wf = HorusWorkflow(
            name="drive",
            tasks=[_task("a", "Alpha")],
            edges=[],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        tui = WorkflowTUISubscriber()
        tui.track(wf, trigger_id="a")

        drive = asyncio.create_task(tui._drive())
        for _ in range(200):
            if tui._finished:
                break
            await asyncio.sleep(0.01)
        assert tui._finished is True

        quit_event.set()
        await asyncio.wait_for(drive, timeout=5)
        assert wf.status.value == "completed"

    async def test_drive_cancels_workflow_when_user_quits_first(
        self,
        monkeypatch: pytest.MonkeyPatch,
        horus_context: Any,
        tmp_path: Any,
    ) -> None:
        """Quitting the app cancels the still-running workflow."""
        del horus_context

        async def _fake_run_async(
            _self: object, *_args: object, **_kwargs: object
        ) -> None:
            await asyncio.sleep(0.05)  # let the workflow dispatch its task

        monkeypatch.setattr(_WorkflowTUIApp, "run_async", _fake_run_async)

        task = _HangingTask(
            id="hang",
            name="Hang",
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
        )
        wf = HorusWorkflow(
            name="drive-cancel",
            tasks=[task],
            edges=[],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )

        with patch.object(
            HorusWorkflow, "transfer_artifacts", new=AsyncMock()
        ):
            tui = WorkflowTUISubscriber()
            tui.track(wf, trigger_id="hang")
            await asyncio.wait_for(tui._drive(), timeout=5)
        assert task._started.is_set()
        assert task.status == TaskStatus.CANCELED

    def test_run_dashboard_falls_back_when_not_interactive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        horus_context: Any,
        tmp_path: Any,
    ) -> None:
        """Non-TTY output skips the dashboard and runs the workflow plainly."""
        del horus_context
        wf = HorusWorkflow(
            name="plain",
            tasks=[_task("a", "Alpha")],
            edges=[],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        tui = WorkflowTUISubscriber()
        tui.track(wf, trigger_id="a")
        monkeypatch.setattr(
            "horus_builtin.event.tui_subscriber._is_interactive",
            lambda: False,
        )
        tui.run_dashboard()
        assert wf.status.value == "completed"

    def test_run_dashboard_interactive_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The interactive path drives the app and prints a receipt."""
        tui = _tracked()
        monkeypatch.setattr(
            "horus_builtin.event.tui_subscriber._is_interactive",
            lambda: True,
        )

        async def _fake_drive(self: WorkflowTUISubscriber) -> None:
            self._finished = True

        monkeypatch.setattr(WorkflowTUISubscriber, "_drive", _fake_drive)
        tui.run_dashboard()
        assert tui._finished is True
        assert tui._started_at is not None
