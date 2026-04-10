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
Local target implementation for executing tasks on the local machine
(in-process).
"""

import asyncio
import socket
from urllib.parse import urlparse

from pydantic import PrivateAttr

from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger


class LocalTarget(BaseTarget):
    """
    Executes tasks directly in the current process.
    """

    kind: str = "local"
    _task: BaseTask | None = PrivateAttr(default=None)
    _task_future: asyncio.Task[None] | None = PrivateAttr(default=None)

    @property
    def location_id(self) -> str:
        """
        All ``LocalTarget`` instances on the same host share the local
        filesystem, so the hostname alone is a sufficient location key.
        """
        return f"local://{socket.gethostname()}"

    async def _dispatch(self, task: BaseTask) -> None:
        """
        Schedule the task as a running ``asyncio.Task`` in the current event
        loop. The task starts executing immediately; call ``wait()`` to block
        until it finishes.

        Raises:
            TaskExecutionError: If the task is already running on this target.
        """
        # TODO: Implement transfer_strategy to ensure artifacts are
        # available to the task.

        # If the task is already running, don't start it again
        if self._task_future is not None and not self._task_future.done():
            raise TaskExecutionError(
                _("Task '%(task_name)s' is already running on this target.")
                % {"task_name": task.name}
            )

        self._task = task
        self._task_future = asyncio.create_task(self._task.run())
        horus_logger.log.debug(
            _("Dispatched task '%(task_name)s' to local target")
            % {"task_name": task.name}
        )

    async def wait(self) -> None:
        """
        Wait for the task to complete.

        Raises:
            TaskExecutionError: If the task has not been dispatched yet.
        """
        if self._task_future is None:
            raise TaskExecutionError(_("Task has not been dispatched yet."))

        horus_logger.log.debug(
            _("Waiting for task '%(task_name)s' to complete on local target")
            % {"task_name": self._task.name if self._task else "unknown"}
        )
        await self._task_future

    async def cancel(self) -> None:
        """
        Cancel the running task by injecting ``CancelledError`` at its next
        ``await`` point, then wait for it to finish any cleanup.
        """
        if self._task_future is None or self._task_future.done():
            return
        horus_logger.log.debug(
            _("Cancelling task '%(task_name)s' on local target")
            % {"task_name": self._task.name if self._task else "unknown"}
        )
        self._task_future.cancel()
        try:
            await self._task_future
        except asyncio.CancelledError:
            pass

    async def get_status(self) -> TaskStatus:
        """
        Get the current status of the task.
        """
        if self._task is None:
            raise TaskExecutionError(_("Task has not been dispatched yet."))
        return self._task.status

    def access_cost(self, artifact: BaseArtifact) -> float | None:
        """
        Return ``0.0`` for artifacts that are natively readable in-process:
        local filesystem URIs (``file://`` scheme or bare paths). Returns
        ``None`` for any other scheme, signalling that a transfer is required.
        """
        parsed = urlparse(artifact.uri)
        if parsed.scheme in ("", "file"):
            return 0.0
        return None
