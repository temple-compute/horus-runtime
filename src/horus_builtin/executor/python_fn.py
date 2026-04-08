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
Python executor for in-memory workflows in horus-runtime. (Function
executor).
"""

from inspect import isawaitable
from typing import ClassVar

from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType
from horus_runtime.core.task.base import BaseTask


class PythonFunctionExecutor(BaseExecutor):
    """
    Executor for running Python functions in-memory.
    """

    kind: str = "python_function"

    runtimes: ClassVar[RuntimeFilterType] = (PythonFunctionRuntime,)

    async def execute(self, task: "BaseTask") -> int:
        """
        Executes the Python function specified in the task's runtime.
        """
        assert isinstance(task.runtime, PythonFunctionRuntime)

        # Get the function from the runtime.
        func, args = task.runtime.setup_runtime(task)

        result = func(**args)

        # If the result is awaitable, await it.
        if isawaitable(result):
            await result

        return 0
