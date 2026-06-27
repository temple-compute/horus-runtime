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

from pathlib import Path

import pytest

from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.middleware.task_log_file import TaskLogFileMiddleware
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.logging import horus_logger
from horus_runtime.middleware.task import TaskMiddlewareContext


class _ConcreteTask(BaseTask):
    kind: str = "test_log_task"
    target: LocalTarget = LocalTarget()

    async def _run(self) -> None:  # pragma: no cover - not exercised here
        pass

    def is_complete(self) -> bool:
        return False

    def _reset(self) -> None:
        pass


def _make_task() -> _ConcreteTask:
    return _ConcreteTask(
        id="t1",
        name="my_task",
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
