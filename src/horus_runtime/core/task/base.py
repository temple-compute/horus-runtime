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

from pydantic import BaseModel, Field

from horus_runtime.registry.artifact_registry import ArtifactUnion
from horus_runtime.registry.auto_registry import AutoRegistry
from horus_runtime.registry.executor_registry import ExecutorUnion
from horus_runtime.registry.runtime_registry import RuntimeUnion


class BaseTask(BaseModel, ABC, AutoRegistry):
    """
    The base task. This class provides the foundational functionality for
    defining and executing tasks, and should be ingested by the executor.
    """

    registry_key: ClassVar[str] = "kind"
    """
    The 'registry_key' field is used to identify the specific type of task.
    """

    kind: Any = ...
    """
    The 'kind' field is used to identify the specific type of task.
    """

    name: str
    """
    Human-readable name for this task.
    """

    inputs: dict[str, ArtifactUnion] = Field(default_factory=dict)
    """
    Input artifacts for this task. These are the artifacts that the task
    depends on.
    """

    outputs: dict[str, ArtifactUnion] = Field(default_factory=dict)
    """
    Output artifacts for this task. These are the artifacts that the task
    produces.
    """

    variables: dict[str, Any] = Field(default_factory=dict)
    """
    Variables for this task. These are the variables that the task can use
    during its execution.
    """

    executor: ExecutorUnion
    """
    The executor that should execute this task. The executor is responsible for
    running the task in the appropriate environment (e.g., locally, on a remote
    server, in a container, etc.).
    """

    runtime: RuntimeUnion
    """
    The runtime that should be used to execute this task. The runtime defines
    the actual command, program or script to run.
    """

    runs: int = 0
    """
    Number of times this task has been run. This can be used for tracking and
    debugging purposes.
    """

    @abstractmethod
    def run(self) -> None:
        """
        Run the task. This method should be implemented by subclasses to define
        the specific logic for running the task based on its context and
        environment.
        """

    @abstractmethod
    def is_complete(self) -> bool:
        """
        Determine whether the task is complete by checking the existence and
        integrity of its output artifacts. This method should be implemented by
        subclasses to define the specific logic for determining task completion
        based on its outputs.
        """

    @abstractmethod
    def reset(self) -> None:
        """
        Reset the task. This allows the task to be re-run from scratch.
        """
