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

import io
from collections.abc import Awaitable, Callable
from contextlib import redirect_stderr, redirect_stdout
from typing import TypeVar

from loguru import logger

from horus_builtin.artifact.file import FileArtifact
from horus_runtime.logging import horus_logger
from horus_runtime.middleware.task import TaskMiddleware, TaskMiddlewareContext

R = TypeVar("R")


class _LoguruStream(io.TextIOBase):
    """Forward in-process ``print()``/stdout to loguru, one line at a time.

    ``print()`` writes to ``sys.stdout``, which loguru never sees, so task
    output was missing from the per-task log file. Routing it through loguru
    sends it to the per-task log file *and* keeps it visible on the terminal
    (loguru's own stdout sink), live, instead of swallowing it. loguru holds
    the original ``sys.stdout``, so forwarding here neither loops nor
    double-prints.
    """

    def __init__(self, level: str) -> None:
        self._level = level
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                logger.log(self._level, line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            logger.log(self._level, self._buf)
            self._buf = ""


class TaskLogFileMiddleware(TaskMiddleware):
    """
    Capture the task's loguru output (plus its stdout/stderr) to its own
    ``<horus_logger.log_directory>/<task.name>.log`` file and register that
    file as a side artifact on the task once it finishes (on success *and*
    failure).
    """

    async def wrap(
        self,
        ctx: TaskMiddlewareContext,
        call_next: Callable[[], Awaitable[R]],
    ) -> R:
        """
        Capture the task's logs to its own file for the duration of the run.
        """
        # Task can be executed remotely, therefore one cannot
        # assume that the task's log file is on the same machine as the
        # executor. For this, we use the orchestrator's logging directory.
        logs_dir = horus_logger.log_directory

        # Create a tmp directory for the task's log file,
        # which will be uploaded as a side artifact.
        safe_name = ctx.task.name.replace("/", "_").replace("\\", "_")

        log_path = logs_dir / f"{safe_name}.log"

        # Add the file to the sink
        handler_id = logger.add(
            sink=log_path,
            format=horus_logger.format,
            level=horus_logger.level,
            enqueue=False,
        )

        # Route the task's print()/stdout (and stderr) through loguru so it
        # lands in the per-task log and stays live on the terminal.
        out_stream = _LoguruStream("INFO")
        err_stream = _LoguruStream("WARNING")

        try:
            with redirect_stdout(out_stream), redirect_stderr(err_stream):
                return await call_next()
        except Exception as e:
            logger.exception(str(e))
            raise
        finally:
            out_stream.flush()
            err_stream.flush()
            logger.remove(handler_id)

            # Create the side artifact for the log file
            logs_artifact = FileArtifact(
                id=f"{ctx.task.id}_logs", path=log_path
            )

            # Add the log file to the task's side artifacts
            ctx.task.side_artifacts.append(logs_artifact)
