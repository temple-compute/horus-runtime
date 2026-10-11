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
"""Tests for the task and workflow time middlewares."""

from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock, patch

import pytest

from horus_builtin.middleware.task_time import TaskTimeMiddleware
from horus_builtin.middleware.workflow_time import WorkflowTimeMiddleware
from horus_runtime.context import HorusContext
from horus_runtime.middleware.task import TaskMiddlewareContext
from horus_runtime.middleware.workflow import WorkflowMiddlewareContext


async def _fail() -> None:
    raise RuntimeError("boom")


async def _succeed() -> None:
    return None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("call_next", "expected"),
    [(_succeed, "Task t completed in"), (_fail, "Task t failed after")],
)
async def test_task_time_message_reflects_outcome(
    call_next: Callable[[], Awaitable[None]], expected: str
) -> None:
    """The elapsed-time message says whether the task failed."""
    ctx = HorusContext.boot()
    task = MagicMock(id="t")
    task.name = "t"
    with patch.object(ctx.bus, "emit") as emit:
        try:
            await TaskTimeMiddleware().wrap(
                TaskMiddlewareContext(task=task), call_next
            )
        except RuntimeError:
            pass
    assert emit.call_args.args[0].message.startswith(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("call_next", "expected"),
    [
        (_succeed, "Workflow w completed in"),
        (_fail, "Workflow w failed after"),
    ],
)
async def test_workflow_time_message_reflects_outcome(
    call_next: Callable[[], Awaitable[None]], expected: str
) -> None:
    """The elapsed-time message says whether the workflow failed."""
    ctx = HorusContext.boot()
    workflow = MagicMock()
    workflow.name = "w"
    with patch.object(ctx.bus, "emit") as emit:
        try:
            await WorkflowTimeMiddleware().wrap(
                WorkflowMiddlewareContext(workflow=workflow),
                call_next,
            )
        except RuntimeError:
            pass
    assert emit.call_args.args[0].message.startswith(expected)
