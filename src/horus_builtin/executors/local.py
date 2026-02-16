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
Defines the LocalExecutor class, which represents an executor that runs a
task locally in the Horus runtime.
"""

import subprocess
from typing import Literal

from horus_runtime.core.executor.base import BaseExecutor


class ExecutionError(Exception):
    """
    Custom exception for errors that occur during task execution.
    """


class LocalExecutor(BaseExecutor):
    """
    Run the tasks locally in the host machine.
    """

    kind: Literal["local"] = "local"

    def execute(self, cmd: str) -> int:
        """
        Runs the task locally in the host machine.

        Args:
            cmd (str): The command to execute.

        Returns:
            int: The return code of the executed command.
        """

        return subprocess.run(
            cmd, shell=True, check=False, text=True
        ).returncode
