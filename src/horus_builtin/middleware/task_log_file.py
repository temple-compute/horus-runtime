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
"""Task middleware that writes a per-task log file as a side-artifact."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from loguru import logger

from horus_runtime.logging import horus_logger
from horus_runtime.middleware.task import TaskMiddleware, TaskMiddlewareContext

R = TypeVar("R")


class TaskLogFileMiddleware(TaskMiddleware):
    """Captures loguru output to {side_artifacts_dir}/{task.name}.log.

    The file is collected and uploaded by the side-product upload middleware
    after the task finishes (on success *and* failure).
    """

    async def wrap(
        self,
        ctx: TaskMiddlewareContext,
        call_next: Callable[[], Awaitable[R]],
    ) -> R:
        """Capture the task's logs to its own file for the duration of the run.

        A task failure is logged *inside* the still-open sink so the
        traceback lands in the task's own ``.log`` and ``api_log_sink``
        forwards it to the workflow log while ``ctx.workflow`` is still set.
        ``CancelledError`` is a ``BaseException`` and is not caught here.
        """
        side_dir = Path(ctx.task.side_artifacts_dir)
        side_dir.mkdir(parents=True, exist_ok=True)
        safe_name = ctx.task.name.replace("/", "_").replace("\\", "_")
        log_path = side_dir / f"{safe_name}.log"
        # ponytail: global loguru sink; logs from concurrent tasks intermix,
        # add per-task filtering if parallel task runs become common.
        handler_id = logger.add(
            sink=str(log_path),
            format=horus_logger.format,
            level=horus_logger.level,
            enqueue=False,
        )
        try:
            return await call_next()
        except Exception:
            logger.exception(f"Task {ctx.task.name} failed")
            raise
        finally:
            logger.remove(handler_id)
