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

import contextlib
from typing import TYPE_CHECKING, ClassVar

from horus_builtin.runtime.python_string import PythonCodeStringRuntime
from horus_runtime.context import HorusContext
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType
from horus_runtime.i18n import tr as _
from horus_runtime.settings import runtime_settings

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class PythonExecExecutor(BaseExecutor):
    """
    Run the tasks locally in the horus-runtime instance.
    """

    kind: str = "python"
    kind_name: ClassVar[str] = "Python Exec"
    kind_description: ClassVar[str] = _(
        "Executes a Python code snippet in-process within the Horus runtime."
    )

    runtimes: ClassVar[RuntimeFilterType] = (PythonCodeStringRuntime,)

    async def _execute(self, task: "BaseTask") -> None:
        """
        Runs the task in-process by executing the Python code specified in the
        task's runtime.
        """
        assert isinstance(task.runtime, PythonCodeStringRuntime)
        code = await task.runtime.setup_runtime(task)

        ctx = HorusContext.get_context()

        # Expose side-artifacts directory to the snippet.
        scope = {
            "ctx": ctx,
            "task": task,
            runtime_settings.SIDE_ARTIFACTS_DIR_ENV: str(
                task.side_artifacts_dir
            ),
        }

        # Security Warning: using exec to execute arbitrary code can be
        # dangerous and should be done with caution.
        # Run in the task's working dir so relative paths match ShellExecutor.
        with contextlib.chdir(task.working_dir):
            exec(code, scope)
