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

from horus_builtin.runtime.python import (
    PythonFunctionReturnType,
    PythonFunctionRuntime,
)
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType
from horus_runtime.core.task.base import BaseTask
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger


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

        A function may return a :class:`BaseArtifact` (or list of them) to
        declare side-artifacts; the returned artifacts stored on
        ``task.side_artifacts``.
        """
        assert isinstance(task.runtime, PythonFunctionRuntime)

        # Get the function and resolved arguments from the runtime.
        func, args = await task.runtime.setup_runtime(task)

        result = func(**args)

        await self._parse_result_artifacts(task, result)

    async def _parse_result_artifacts(
        self, task: BaseTask, result: PythonFunctionReturnType
    ) -> None:
        """
        Parse the result of a Python function execution to extract any declared
        side-product artifacts.
        """
        if isawaitable(result):
            result = await result

        if result is None:
            return
        if isinstance(result, BaseArtifact):
            task.side_artifacts = [result]
            return
        if isinstance(result, list) and all(
            isinstance(r, BaseArtifact) for r in result
        ):
            task.side_artifacts = result
            return

        horus_logger.log.warning(
            _(
                "Task %(task_id)s returned an unexpected value from its "
                "Python function runtime. Expected BaseArtifact, "
                "list[BaseArtifact], or None; got: %(result)s. Skipping side "
                "artifact handling."
            )
            % {"task_id": task.id, "result": result}
        )
