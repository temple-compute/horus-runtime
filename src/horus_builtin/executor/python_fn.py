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
from horus_runtime.i18n import tr as _


class PythonFunctionExecutor(BaseExecutor):
    """
    Executor for running Python functions in-memory.
    """

    kind: str = "python_function"
    kind_name: ClassVar[str] = "Python Function Executor"
    kind_description: ClassVar[str] = _(
        "Executes a Python function in-memory within the Horus runtime."
    )

    runtimes: ClassVar[RuntimeFilterType] = (PythonFunctionRuntime,)

    async def _execute(self, task: "BaseTask") -> None:
        """
        Executes the Python function specified in the task's runtime.
        """
        assert isinstance(task.runtime, PythonFunctionRuntime)

        # Get the function and resolved arguments from the runtime.
        func, args = await task.runtime.setup_runtime(task)

        result = func(**args)

        if isawaitable(result):
            await result
