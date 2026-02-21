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
Default Horus task implementation.
"""

from typing import Literal

from horus_runtime.core.artifact.exceptions import ArtifactDoesNotExistError
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.exceptions import TaskExecutionError


class HorusTask(BaseTask):
    """
    The HorusTask represents a basic task in the Horus runtime. This task is
    designed to be executed by the CommandExecutor, and simply runs the command
    specified in the runtime.
    """

    kind: Literal["horus_task"] = "horus_task"

    def run(self):
        """
        For a HorusTask, nothing needs to be done here, as the command is
        already specified in the runtime and will be executed by the executor.
        """

        # Gather inputs
        for input_name, artifact in self.inputs.items():
            print(f"Input {input_name}: {artifact}")

            if not artifact.exists():
                raise ArtifactDoesNotExistError(
                    f"Input artifact {input_name} does not exist"
                )

        # Execute the command using the executor
        return_code = self.executor.execute(self)

        if return_code != 0:
            raise TaskExecutionError(
                f"Task execution failed with return code {return_code}"
            )
