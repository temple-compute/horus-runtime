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
from typing import Any, Self

from pydantic import model_validator

from horus_builtin.executor.python_fn import PythonFunctionExecutor
from horus_builtin.interaction.cli import CLIInteractionTransport
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.interaction.transport import BaseInteractionTransport
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.workflow.base import BaseWorkflow


class FunctionTask(HorusTask):
    """
    A simple task that executes a Python function. This is a basic wrapper
    around HorusTask that can be used for tasks defined by Python functions.
    """

    kind: str = "function_task"

    task_id: str = ""
    """
    Override task_id to be derived from the function name. This ensures that
    the task_id is consistent with the workflow key when using the decorator.
    """

    runtime: PythonFunctionRuntime
    executor: PythonFunctionExecutor = PythonFunctionExecutor()

    # Default to CLI transport for interactions in FunctionTasks.
    interaction: BaseInteractionTransport = CLIInteractionTransport()

    @model_validator(mode="after")
    def sync_task_id(self) -> Self:
        """
        Ensure that the task_id is consistent with the name, which is derived
        from the function name. This is important for decorator-registered
        tasks to ensure their task_id matches the workflow key.
        """
        self.task_id = self.name
        return self

    @staticmethod
    def task(
        wf: BaseWorkflow,
        *,
        name: str | None = None,
        inputs: dict[str, BaseArtifact] | None = None,
        outputs: dict[str, BaseArtifact] | None = None,
        target: BaseTarget | None = None,
    ) -> Callable[[Callable[..., Any]], "FunctionTask"]:
        """
        Decorator factory for registering a Python function as a Horus task
        within a workflow.

        Usage:
            @FunctionTask.task(wf, inputs={"data": my_artifact})
            def process(data: FileArtifact) -> None: ...
        """

        def decorator(func: Callable[..., Any]) -> "FunctionTask":
            t = FunctionTask(
                name=name or func.__name__,
                runtime=PythonFunctionRuntime(func=func),
                inputs=inputs or {},
                outputs=outputs or {},
                target=target or LocalTarget(),
            )

            wf.tasks[t.task_id] = t

            return t

        return decorator
