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
task locally in the Horus runtime.
"""

import asyncio
from typing import TYPE_CHECKING, ClassVar

from horus_builtin.runtime.command import CommandRuntime
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class ShellExecutor(BaseExecutor):
    """
    Run the tasks locally in the host machine.
    """

    kind: str = "shell"

    runtimes: ClassVar[RuntimeFilterType] = (CommandRuntime,)

    async def _execute(self, task: "BaseTask") -> None:
        """
        Runs the task locally in the host machine.
        """
        assert isinstance(task.runtime, CommandRuntime)
        prepared_command = await task.runtime.setup_runtime(task)

        horus_logger.log.debug(
            _("Executing command for task %(task_id)s: %(command)s")
            % {"task_id": task.id, "command": prepared_command}
        )

        # Security Warning:
        # This method uses a shell to execute the prepared command, which
        # poses a security risk if `cmd` contains untrusted input. Shell
        # injection attacks are possible if user-supplied data is passed
        # directly to this method. It is the caller's responsibility to ensure
        # that `cmd` is properly sanitized and does not contain malicious
        # content. The local runtime intentionally allows this "free for all"
        # for maximum flexibility, assuming the user is executing commands on
        # their own machine.
        process = await asyncio.create_subprocess_shell(
            prepared_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            __, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise

        if process.returncode != 0:
            horus_logger.log.error(
                _(
                    "Command execution failed for task %(task_id)s with "
                    "return code %(return_code)s. Stderr: %(stderr)s"
                )
                % {
                    "task_id": task.id,
                    "return_code": process.returncode,
                    "stderr": stderr.decode().strip(),
                }
            )
            raise TaskExecutionError(
                _("Shell command exited with return code %(return_code)s")
                % {"return_code": process.returncode}
            )
