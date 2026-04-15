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

import subprocess
from typing import TYPE_CHECKING, ClassVar

from horus_builtin.runtime.command import CommandRuntime
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType
from horus_runtime.logging import horus_logger

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class ShellExecutor(BaseExecutor):
    """
    Run the tasks locally in the host machine.
    """

    kind: str = "shell"

    runtimes: ClassVar[RuntimeFilterType] = (CommandRuntime,)

    async def execute(self, task: "BaseTask") -> int:
        """
        Runs the task locally in the host machine.

        Args:
            task (BaseTask): The task to execute.

        Returns:
            int: The return code of the executed command.
        """
        assert isinstance(task.runtime, CommandRuntime)
        prepared_command = task.runtime.setup_runtime(task)

        horus_logger.log.debug(
            f"Executing command for task {task.task_id}: {prepared_command}"
        )

        # Security Warning:
        # This method uses `shell=True` with `subprocess.run`, which poses a
        # security risk if `cmd` contains untrusted input. Shell injection
        # attacks are possible if user-supplied data is passed directly to this
        # method. It is the caller's responsibility to ensure that `cmd` is
        # properly sanitized and does not contain malicious content.
        # The local runtime intentionally allows this "free for all" for
        # maximum flexibility, assuming the user is executing commands on
        # their own machine.
        p = subprocess.run(
            prepared_command,
            shell=True,
            check=False,
            text=True,
            capture_output=True,
        )

        # Log the command's stdout and stderr for debugging purposes
        if p.stdout:
            horus_logger.log.debug(
                f"Command stdout for task {task.task_id}:\n{p.stdout}"
            )
        if p.stderr:
            horus_logger.log.debug(
                f"Command stderr for task {task.task_id}:\n{p.stderr}"
            )

        return p.returncode
