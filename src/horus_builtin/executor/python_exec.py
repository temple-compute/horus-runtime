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
Defines the PythonExecExecutor class, which represents an executor that runs a
a Python code task in-process in the Horus runtime.
"""

import traceback
from typing import TYPE_CHECKING, ClassVar

from horus_builtin.runtime.python_string import PythonCodeStringRuntime
from horus_runtime.context import HorusContext
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class PythonExecExecutor(BaseExecutor):
    """
    Run the tasks locally in the horus-runtime instance.
    """

    kind: str = "python"

    runtimes: ClassVar[RuntimeFilterType] = (PythonCodeStringRuntime,)

    async def execute(self, task: "BaseTask") -> int:
        """
        Runs the task in-process by executing the Python code specified in the
        task's runtime.
        """
        assert isinstance(task.runtime, PythonCodeStringRuntime)
        code = task.runtime.setup_runtime(task)

        ctx = HorusContext.get_context()

        scope = {
            "ctx": ctx,
            "task": task,
        }

        try:
            # Security Warning: using exec to execute arbitrary code can be
            # dangerous and should be done with caution.
            exec(code, scope)
            return 0
        except Exception:
            traceback.print_exc()
            return 1
