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
Base runtime. The runtime represents the command, environment, and other
context in which a task is executed. The base runtime provides the foundational
functionality for executing tasks, and should be ingested by the executor.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from horus_runtime.core.registry.auto_registry import AutoRegistry

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class BaseRuntime(BaseModel, ABC, AutoRegistry):
    """
    The base runtime. This class provides the foundational functionality for
    executing tasks, and should be ingested by the executor.
    """

    registry_key: ClassVar[str] = "kind"

    kind: Any = None
    """
    The 'kind' field is used to identify the specific type of runtime.
    """

    @abstractmethod
    def _setup_runtime(self, task: "BaseTask") -> str:
        """
        Prepare the runtime to execute. This method should be implemented by
        subclasses to define the specific logic for preparing the command/task
        based on the runtime's context and environment.

        Returns:
            str: The prepared runtime instance.
        """

    def format_runtime(self, task: "BaseTask") -> str:
        """
        Format the runtime's command or context by substituting any variables
        using the task's variables and inputs. This method can be overridden by
        subclasses if they need custom formatting logic, but by default it will
        simply call _setup_runtime to perform the variable substitution.

        Returns:
            str: The formatted command or context ready for execution.
        """

        cmd = self._setup_runtime(task)

        # Create a namespace object to allow for attribute-style access to task
        # variables and inputs in the command formatting. This allows users to
        # write commands like "echo {task.input1.path}" in the workflow yaml
        class _TaskNamespace:
            def __init__(self, task: "BaseTask"):
                for name, value in vars(task).items():
                    setattr(self, name, value)
                for name, artifact in task.inputs.items():
                    setattr(self, name, artifact)
                for name, artifact in task.outputs.items():
                    setattr(self, name, artifact)

        fmt_kwargs = {
            "task": _TaskNamespace(task),
            **task.inputs,
            **task.outputs,
            **task.variables,
        }

        return cmd.format(**fmt_kwargs)
