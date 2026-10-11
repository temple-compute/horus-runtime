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
Builtin plugin to time task execution.
"""

import time
from asyncio import CancelledError
from collections.abc import Awaitable, Callable
from typing import TypeVar

from horus_builtin.event.task_event import HorusTaskEvent
from horus_runtime.context import HorusContext
from horus_runtime.i18n import tr as _
from horus_runtime.middleware.task import TaskMiddleware, TaskMiddlewareContext

R = TypeVar("R")


class TaskTimeMiddleware(TaskMiddleware):
    """
    Simple middleware to time task execution.
    """

    async def wrap(
        self,
        middleware_context: TaskMiddlewareContext,
        call_next: Callable[[], Awaitable[R]],
    ) -> R:
        """
        Time the task and report the elapsed time with how it ended.
        """
        start = time.perf_counter()
        message = _("Task %(task_name)s failed after %(elapsed).2f seconds.")
        try:
            result = await call_next()
            message = _(
                "Task %(task_name)s completed in %(elapsed).2f seconds."
            )
            return result
        except CancelledError:
            message = _(
                "Task %(task_name)s was canceled after %(elapsed).2f seconds."
            )
            raise
        finally:
            elapsed = time.perf_counter() - start
            task = middleware_context.task
            HorusContext.get_context().bus.emit(
                HorusTaskEvent(
                    task_id=task.id,
                    task_name=task.name,
                    data={"elapsed_time": elapsed},
                    message=message
                    % {"task_name": task.name, "elapsed": elapsed},
                )
            )
