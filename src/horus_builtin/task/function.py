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
Specific task implementations for in-memory workflows in horus-runtime.
"""

from collections.abc import Callable
from typing import Any

from horus_builtin.executor.python_fn import PythonFunctionExecutor
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.workflow.base import BaseWorkflow


class FunctionTask(HorusTask):
    """
    A simple task that executes a Python function. This is a basic wrapper
    around HorusTask that can be used for tasks defined by Python functions.
    """

    kind: str = "function_task"

    runtime: PythonFunctionRuntime
    executor: PythonFunctionExecutor = PythonFunctionExecutor()

    @staticmethod
    def task(
        wf: BaseWorkflow,
        *,
        name: str | None = None,
        inputs: dict[str, BaseArtifact] | None = None,
        outputs: dict[str, BaseArtifact] | None = None,
    ) -> Callable[[Callable[..., Any]], "FunctionTask"]:
        """
        Decorator factory. The natural home for this is here — FunctionTask
        owns the construction of runtime + executor together.

            @FunctionTask.task(wf, inputs={"data": my_artifact})
            def process(data: FileArtifact) -> None: ...
        """

        def decorator(func: Callable[..., Any]) -> "FunctionTask":
            t = FunctionTask(
                name=name or func.__name__,
                runtime=PythonFunctionRuntime(func=func),
                inputs=inputs or {},
                outputs=outputs or {},
            )

            wf.tasks[t.name] = t
            return t

        return decorator
