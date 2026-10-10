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
"""Tests for TaskLogFileMiddleware."""

import asyncio
from pathlib import Path

import pytest
from loguru import logger

from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.middleware.task_log_file import TaskLogFileMiddleware
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_runtime.context import running_task
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.logging import horus_logger
from horus_runtime.middleware.task import TaskMiddlewareContext


class _ConcreteTask(BaseTask):
    kind: str = "test_log_task"
    target: BaseTarget = LocalTarget()

    async def _run(self) -> None:
        pass

    async def is_complete(self) -> bool:
        return False

    async def _reset(self) -> None:
        pass


def _make_task(task_id: str = "t1", name: str = "my_task") -> _ConcreteTask:
    return _ConcreteTask(
        id=task_id,
        name=name,
        runtime=CommandRuntime(command="echo hi"),
        executor=ShellExecutor(),
        target=LocalTarget(),
    )


@pytest.mark.unit
async def test_log_artifact_registered_before_task_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The log side-artifact must be present in ``task.side_artifacts`` *while*
    the wrapped callable runs, not only after. A side-product upload middleware
    can sit inside this one in the chain and read ``side_artifacts`` in its own
    ``finally`` — which fires before ours — so registering after the fact would
    drop the log from the upload.
    """
    monkeypatch.setattr(horus_logger, "log_directory", tmp_path)
    task = _make_task()
    ctx = TaskMiddlewareContext(task=task)

    seen_during_run: list[str] = []

    async def call_next() -> str:
        seen_during_run.extend(a.id for a in task.side_artifacts)
        return "ok"

    result = await TaskLogFileMiddleware().wrap(ctx, call_next)

    assert result == "ok"
    assert f"{task.id}_logs" in seen_during_run


@pytest.mark.unit
async def test_concurrent_tasks_log_only_their_own_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Loguru sinks are global: each task's log file must only collect the lines
    logged while that task is the current one, not its concurrent siblings'.
    """
    monkeypatch.setattr(horus_logger, "log_directory", tmp_path)

    async def run(task: _ConcreteTask) -> None:
        async def call_next() -> None:
            for i in range(3):
                logger.info(f"line from {task.name} {i}")
                await asyncio.sleep(0)

        # Mirror BaseTask.run, which sets the current task around the chain.
        with running_task(task.id):
            await TaskLogFileMiddleware().wrap(
                TaskMiddlewareContext(task=task), call_next
            )

    await asyncio.gather(
        run(_make_task("a", "task_a")), run(_make_task("b", "task_b"))
    )
    logger.info("workflow-level line")

    log_a = (tmp_path / "task_a.log").read_text()
    log_b = (tmp_path / "task_b.log").read_text()
    assert log_a.count("line from task_a") == 3
    assert log_b.count("line from task_b") == 3
    assert "task_b" not in log_a
    assert "task_a" not in log_b
