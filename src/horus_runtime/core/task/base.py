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

from abc import abstractmethod
from typing import Any, ClassVar, Self

from pydantic import Field, model_validator

from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.executor.exceptions import IncompatibleRuntimeError
from horus_runtime.core.interaction.transport import BaseInteractionTransport
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.registry.auto_registry import AutoRegistry


class BaseTask(AutoRegistry, entry_point="task"):
    """
    The base task. This class provides the foundational functionality for
    defining and executing tasks, and should be ingested by the executor.
    """

    registry_key: ClassVar[str] = "kind"
    """
    The 'registry_key' field is used to identify the specific type of task.
    """

    kind: str
    """
    The 'kind' field is used to identify the specific type of task.
    """

    task_id: str | None = None
    """
    The task ID
    """

    name: str
    """
    Human-readable name for this task.
    """

    inputs: dict[str, BaseArtifact] = Field(default_factory=dict)
    """
    Input artifacts for this task. These are the artifacts that the task
    depends on.
    """

    outputs: dict[str, BaseArtifact] = Field(default_factory=dict)
    """
    Output artifacts for this task. These are the artifacts that the task
    produces.
    """

    variables: dict[str, Any] = Field(default_factory=dict)
    """
    Variables for this task. These are the variables that the task can use
    during its execution.
    """

    executor: BaseExecutor
    """
    The executor that should execute this task. The executor is responsible for
    running the task in the appropriate environment (e.g., locally, on a remote
    server, in a container, etc.).
    """

    runtime: BaseRuntime
    """
    The runtime that should be used to execute this task. The runtime defines
    the actual command, program or script to run.
    """

    runs: int = 0
    """
    Number of times this task has been run. This can be used for tracking and
    debugging purposes.
    """

    skip_if_complete: bool = True
    """
    Whether to skip execution of this task if it is already complete.
    """

    interaction: BaseInteractionTransport | None = Field(
        default=None,
        exclude=True,
    )
    """
    The interaction transport currently associated with the task run.
    """

    @model_validator(mode="after")
    def _validate_runtime_compatibility(self) -> Self:
        if not isinstance(self.runtime, self.executor.runtimes):
            raise IncompatibleRuntimeError(
                f"Runtime '{type(self.runtime).__name__}' is not compatible "
                f"with executor '{type(self.executor).__name__}'. "
                f"Expected one of: "
                f"{[r.__name__ for r in self.executor.runtimes]}"
            )
        return self

    @abstractmethod
    async def run(self) -> None:
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
