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
Python runtime implementation for in-memory workflows.
"""

from collections.abc import Callable
from inspect import Parameter, signature
from typing import Any

from pydantic import ConfigDict, Field

from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.task.base import BaseTask

PythonFunctionSetupTuple = tuple[Callable[..., Any], dict[str, Any]]


class PythonFunctionRuntime(BaseRuntime[PythonFunctionSetupTuple]):
    """
    Executes a python function.
    """

    kind: str = "python_function"

    # Allow callable types in the runtime configuration
    model_config = ConfigDict(arbitrary_types_allowed=True)

    func: Callable[..., Any] = Field(..., exclude=True)

    def setup_runtime(self, task: "BaseTask") -> PythonFunctionSetupTuple:
        """
        Prepare the runtime for execution. Checks the function signature,
        ensuring that all parameters are accounted for by the task's inputs,
        outputs, or variables.
        """
        # Get the function signature (args and kwargs)
        sig = signature(self.func)

        # Define the allowed parameter names for the function:
        # task, inputs, outputs, variables
        kwargs = {
            "task": task,
            **task.inputs,
            **task.outputs,
            **task.variables,
        }

        # Check that all parameters in the function signature are accounted for
        accepts_kwargs = any(
            param.kind is Parameter.VAR_KEYWORD  # literally '**kwargs'
            for param in sig.parameters.values()
        )

        # If the function accepts **kwargs, we can pass all available kwargs.
        # Otherwise, we filter to only the parameters explicitly defined in the
        # function signature.
        if accepts_kwargs:
            call_kwargs = kwargs
        else:
            # Only pass the kwargs that match the function signature
            # parameters.
            call_kwargs = {
                name: value
                for name, value in kwargs.items()
                if name in sig.parameters
            }

        return self.func, call_kwargs
