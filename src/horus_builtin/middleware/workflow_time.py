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
Builtin plugin to time workflow execution.
"""

import time

from horus_builtin.event.workflow_event import HorusWorkflowEvent
from horus_runtime.context import HorusContext
from horus_runtime.i18n import tr as _
from horus_runtime.middleware.workflow import (
    WorkflowMiddleware,
    WorkflowMiddlewareContext,
)


class WorkflowTimeMiddleware(WorkflowMiddleware):
    """
    Simple middleware to time workflow execution.
    """

    _time_start: float = 0.0

    async def before(self, _: WorkflowMiddlewareContext) -> None:
        """
        Start the timer.
        """
        self._time_start = time.perf_counter()

    async def after(
        self, middleware_context: WorkflowMiddlewareContext
    ) -> None:
        """
        Stop the timer and print the elapsed time.
        """
        elapsed = time.perf_counter() - self._time_start

        ctx = HorusContext.get_context()
        ctx.bus.emit(
            HorusWorkflowEvent(
                workflow_name=middleware_context.workflow.name,
                data={"elapsed_time": elapsed},
                message=_(
                    "Workflow %(workflow_name)s completed in"
                    " %(elapsed).2f seconds."
                )
                % {
                    "workflow_name": middleware_context.workflow.name,
                    "elapsed": elapsed,
                },
            )
        )
