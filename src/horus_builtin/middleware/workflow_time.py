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
from asyncio import CancelledError
from collections.abc import Awaitable, Callable
from typing import TypeVar

from horus_builtin.event.workflow_event import HorusWorkflowEvent
from horus_runtime.context import HorusContext
from horus_runtime.i18n import tr as _
from horus_runtime.middleware.workflow import (
    WorkflowMiddleware,
    WorkflowMiddlewareContext,
)

R = TypeVar("R")


class WorkflowTimeMiddleware(WorkflowMiddleware):
    """
    Simple middleware to time workflow execution.
    """

    async def wrap(
        self,
        middleware_context: WorkflowMiddlewareContext,
        call_next: Callable[[], Awaitable[R]],
    ) -> R:
        """
        Time the workflow and report the elapsed time with how it ended.

        ``BaseWorkflow.run`` sets the terminal status only after the
        middleware chain returns, so the outcome is read from ``call_next``.
        """
        start = time.perf_counter()
        message = _(
            "Workflow %(workflow_name)s failed after %(elapsed).2f seconds."
        )
        try:
            result = await call_next()
            message = _(
                "Workflow %(workflow_name)s completed in"
                " %(elapsed).2f seconds."
            )
            return result
        except CancelledError:
            message = _(
                "Workflow %(workflow_name)s was canceled after"
                " %(elapsed).2f seconds."
            )
            raise
        finally:
            elapsed = time.perf_counter() - start
            name = middleware_context.workflow.name
            HorusContext.get_context().bus.emit(
                HorusWorkflowEvent(
                    workflow_name=name,
                    data={"elapsed_time": elapsed},
                    message=message
                    % {"workflow_name": name, "elapsed": elapsed},
                )
            )
