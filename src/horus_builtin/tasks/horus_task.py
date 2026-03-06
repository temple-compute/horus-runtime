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
    The HorusTask represents a basic task in the Horus runtime.
    """

    kind: Literal["horus_task"] = "horus_task"

    def run(self) -> None:
        """
        For a HorusTask, nothing needs to be done here, as the command is
        already specified in the runtime and will be executed by the executor.
        """

        self.runs += 1

        # Gather inputs
        for (
            input_name,
            artifact,
        ) in self.inputs.items():
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

    def is_complete(self) -> bool:
        """
        A HorusTask is considered complete if all of its output artifacts
        exist.
        """

        # If no outputs are declared, we consider the task incomplete and
        # always run it
        if not self.outputs:
            return False

        for artifact in self.outputs.values():
            if not artifact.exists():
                return False

        return True

    def reset(self) -> None:
        """
        Reset the task by deleting all output artifacts. This allows the task
        to be re-run from scratch.
        """

        for artifact in self.outputs.values():
            artifact.delete()

        self.runs = 0
