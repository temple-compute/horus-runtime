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
Defines the ShellExecutor class, which represents an executor that runs a
task via the target's channel (``run_command``).

``ShellExecutor`` drives the agentless channel: it renders the command via the
task's runtime, then delegates execution to ``task.target.run_command`` rather
than spawning a subprocess directly.  This means the same executor works
identically on ``LocalTarget`` and on any future remote target (SSH, etc.)
without modification.
"""

import asyncio
from typing import TYPE_CHECKING, ClassVar

from horus_builtin.runtime.command import CommandRuntime
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.settings import runtime_settings

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class ShellExecutor(BaseExecutor):
    """
    Run the task via the target channel (``run_command``).

    The executor renders the command through the task's
    :class:`~horus_builtin.runtime.command.CommandRuntime`, passes the
    per-task side-artifacts directory as an environment variable, and drives
    the returned :class:`~horus_runtime.core.target.channel.ChannelProcess`
    handle.  Cancellation kills the entire process group so no orphaned
    children are left behind.
    """

    kind: str = "shell"
    kind_name: ClassVar[str] = "Shell Executor"
    kind_description: ClassVar[str] = _(
        "Executes a shell command via the target channel."
    )

    runtimes: ClassVar[RuntimeFilterType] = (CommandRuntime,)

    async def _execute(self, task: "BaseTask") -> None:
        """
        Render the command and run it through the target channel.

        Args:
            task: The task to execute.  ``task.runtime`` must be a
                :class:`~horus_builtin.runtime.command.CommandRuntime`.

        Raises:
            TaskExecutionError: If the command exits with a non-zero status.
        """
        assert isinstance(task.runtime, CommandRuntime)
        prepared_command = await task.runtime.setup_runtime(task)

        horus_logger.log.debug(
            _("Executing command for task %(task_id)s: %(command)s")
            % {"task_id": task.id, "command": prepared_command}
        )

        # Let scripts drop side-product files in a well-known directory.
        env = {
            runtime_settings.SIDE_ARTIFACTS_DIR_ENV: str(
                task.side_artifacts_dir
            ),
        }

        # Security Warning:
        # This method uses a shell to execute the prepared command, which
        # poses a security risk if ``cmd`` contains untrusted input. Shell
        # injection attacks are possible if user-supplied data is passed
        # directly to this method. It is the caller's responsibility to ensure
        # that ``cmd`` is properly sanitised and does not contain malicious
        # content. The local runtime intentionally allows this "free for all"
        # for maximum flexibility, assuming the user is executing commands on
        # their own machine.
        proc = await task.target.run_command(
            prepared_command,
            cwd=task.working_dir,
            env=env,
        )

        try:
            __, stderr = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            horus_logger.log.error(
                _(
                    "Command execution failed for task %(task_id)s with "
                    "return code %(return_code)s. Stderr: %(stderr)s"
                )
                % {
                    "task_id": task.id,
                    "return_code": proc.returncode,
                    "stderr": stderr.decode().strip(),
                }
            )
            raise TaskExecutionError(
                _("Shell command exited with return code %(return_code)s")
                % {"return_code": proc.returncode}
            )
