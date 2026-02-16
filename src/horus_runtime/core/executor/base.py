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
Defines the Executor base class, which represents an executor in the Horus
runtime. An executor is on charge of actually running the task, by using the
specified runtime in a certain environment, for example running it locally as
a command or running it inside a SLURM job, either remote or locally.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from horus_runtime.core.registry.auto_registry import AutoRegistry


class BaseExecutor(BaseModel, ABC, AutoRegistry):
    """
    The base executor represents the abstract concept of an executor in the
    Horus runtime. An executor is on charge of actually running the task in the
    designated runtime and environment.
    """

    registry_key: ClassVar[str] = "kind"

    kind: Any = None
    """
    The 'kind' field is used to identify the specific type of executor.
    """

    @abstractmethod
    def execute(self, cmd: str) -> int:
        """
        Execute the task using the specified runtime and environment.
        This method should be implemented by subclasses to define the specific
        execution logic for different types of executors.

        Args:
            cmd (str): The command to execute.

        Returns:
            int: The return code of the execution.
        """
