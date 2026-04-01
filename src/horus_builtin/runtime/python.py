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
from typing import Any

from pydantic import ConfigDict, Field

from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.task.base import BaseTask


class PythonFunctionRuntime(BaseRuntime[Callable[..., Any]]):
    """
    Executes a python function.
    """

    kind: str = "python_function"

    # Allow callable types in the runtime configuration
    model_config = ConfigDict(arbitrary_types_allowed=True)

    func: Callable[..., Any] = Field(..., exclude=True)

    def setup_runtime(self, task: "BaseTask") -> Callable[..., Any]:
        """
        Nothing to be done for the PythonFunctionRuntime.
        """
        return self.func
