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

from horus_builtin.event.task_event import HorusTaskEvent
from horus_runtime.context import HorusContext
from horus_runtime.i18n import tr as _
from horus_runtime.middleware.task import TaskMiddleware, TaskMiddlewareContext


class TaskTimeMiddleware(TaskMiddleware):
    """
    Simple middleware to time task execution.
    """

    _time_start: float = 0.0

    async def before(self, _: TaskMiddlewareContext) -> None:
        """
        Start the timer.
        """
        self._time_start = time.perf_counter()

    async def after(self, middleware_context: TaskMiddlewareContext) -> None:
        """
        Stop the timer and print the elapsed time.
        """
        elapsed = time.perf_counter() - self._time_start

        ctx = HorusContext.get_context()
        ctx.bus.emit(
            HorusTaskEvent(
                task_id=middleware_context.task.id,
                task_name=middleware_context.task.name,
                data={"elapsed_time": elapsed},
                message=_(
                    "Task %(task_name)s completed in %(elapsed).2f seconds."
                )
                % {
                    "task_name": middleware_context.task.name,
                    "elapsed": elapsed,
                },
            )
        )
