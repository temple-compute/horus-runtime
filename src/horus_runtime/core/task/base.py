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
Base Task. The task represents the unit of work that is executed by the Horus
runtime. The base task provides the foundational functionality for defining and
executing tasks, and should be ingested by the executor.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from horus_runtime.core.registry.artifact_registry import ArtifactUnion
from horus_runtime.core.registry.auto_registry import AutoRegistry
from horus_runtime.core.registry.executor_registry import ExecutorUnion
from horus_runtime.core.registry.runtime_registry import RuntimeUnion


class BaseTask(BaseModel, ABC, AutoRegistry):
    """
    The base task. This class provides the foundational functionality for
    defining and executing tasks, and should be ingested by the executor.
    """

    registry_key: ClassVar[str] = "kind"
    """
    The 'registry_key' field is used to identify the specific type of task.
    """

    kind: Any = None
    """
    The 'kind' field is used to identify the specific type of task.
    """

    inputs: dict[str, ArtifactUnion] = {}

    outputs: dict[str, ArtifactUnion] = {}

    variables: dict[str, Any] = {}

    executor: ExecutorUnion

    runtime: RuntimeUnion

    @abstractmethod
    def run(self):
        """
        Run the task. This method should be implemented by subclasses to define
        the specific logic for running the task based on its context and
        environment.
        """
