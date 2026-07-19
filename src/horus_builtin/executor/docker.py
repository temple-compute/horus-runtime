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
Docker executor: runs tasks inside a Docker container.

Container IDs are tracked on the executor instance so that
:meth:`~horus_runtime.core.target.base.BaseTarget.cancel` can call
``docker stop <id>`` and avoid leaving orphaned containers.
"""

import asyncio
import shlex
from typing import TYPE_CHECKING, ClassVar

from pydantic import PrivateAttr

from horus_builtin.runtime.command import CommandRuntime
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class DockerExecutor(BaseExecutor):
    """
    Execute a task's command runtime inside a Docker container.

    The container ID is captured as soon as ``docker run`` starts and stored
    in :attr:`_container_id` so that :meth:`stop_running_container` can issue
    ``docker stop`` on cancellation, preventing orphaned ``docker run``
    processes (Bug #72).
    """

    kind: str = "docker"
    kind_name: ClassVar[str] = "Docker Executor"
    kind_description: ClassVar[str] = _(
        "Executes a shell command inside a Docker container."
    )

    runtimes: ClassVar[RuntimeFilterType] = (CommandRuntime,)

    image: str
    """
    Docker image to use for the container (e.g. ``"python:3.12-slim"``).
    """

    _container_id: str | None = PrivateAttr(default=None)
    """
    ID of the running container, set after ``docker run`` starts.
    ``None`` when no container is active.
    """

    async def _execute(self, task: "BaseTask") -> None:
        """
        Run the task's command inside a Docker container.

        Uses ``docker run --rm`` so the container is automatically removed on
        exit.  The container ID is captured from ``docker run``'s stdout and
        stored for :meth:`stop_running_container`.

        Args:
            task: The task to execute.  ``task.runtime`` must be a
                :class:`~horus_builtin.runtime.command.CommandRuntime`.

        Raises:
            TaskExecutionError: If the container exits with a non-zero status.
        """
        assert isinstance(task.runtime, CommandRuntime)
        prepared_command = await task.runtime.setup_runtime(task)

        horus_logger.log.debug(
            _("Running Docker container for task %(task_id)s: %(cmd)s")
            % {"task_id": task.id, "cmd": prepared_command}
        )

        # Launch container detached so we can capture its ID, then stream
        # logs separately.
        docker_run = (
            f"docker run -d --rm"
            f" -w {shlex.quote(task.working_dir)}"
            f" {shlex.quote(self.image)}"
            f" sh -c {shlex.quote(prepared_command)}"
        )

        # Capture container ID.
        id_proc = await asyncio.create_subprocess_shell(
            docker_run,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await id_proc.communicate()
        if id_proc.returncode != 0:
            raise TaskExecutionError(
                _("docker run failed: %(err)s")
                % {"err": stderr.decode("utf-8", errors="replace").strip()}
            )

        self._container_id = stdout.decode().strip()
        horus_logger.log.debug(
            _("Container started: %(cid)s") % {"cid": self._container_id}
        )

        # Stream logs.
        logs_proc = await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            "-f",
            self._container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            if logs_proc.stdout:
                async for line in logs_proc.stdout:
                    horus_logger.log.info(
                        line.decode("utf-8", errors="replace").rstrip()
                    )
        except asyncio.CancelledError:
            logs_proc.terminate()
            raise

        # Wait for the container to finish.
        wait_proc = await asyncio.create_subprocess_exec(
            "docker",
            "wait",
            self._container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        wait_stdout, _wait_stderr = await wait_proc.communicate()
        exit_code = int(wait_stdout.decode().strip() or "0")
        self._container_id = None

        if exit_code != 0:
            raise TaskExecutionError(
                _("Docker container exited with code %(rc)s")
                % {"rc": exit_code}
            )

    async def stop_running_container(self) -> None:
        """
        Stop the Docker container started by this executor, if any.

        Called by :meth:`~horus_runtime.core.target.base.BaseTarget.cancel`
        before injecting ``CancelledError`` so the container is terminated
        immediately rather than waiting for it to finish on its own.
        """
        if self._container_id is None:
            return
        container_id = self._container_id
        self._container_id = None
        horus_logger.log.debug(
            _("Stopping container %(cid)s") % {"cid": container_id}
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "stop",
                container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception as exc:
            horus_logger.log.warning(
                _("Failed to stop container %(cid)s: %(err)s")
                % {"cid": container_id, "err": exc}
            )
