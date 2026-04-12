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
Unit tests for FunctionTask.
"""

from pathlib import Path

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.python_fn import PythonFunctionExecutor
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.task.function import FunctionTask
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.core.task.base import BaseTask


@pytest.mark.unit
class TestInitRegistry:
    """
    Test that FunctionTask is properly registered.
    """

    def test_init_registry_scans_function_task(self) -> None:
        """
        Test that init_registry properly registers the function_task kind.
        """
        assert "function_task" in BaseTask.registry
        assert BaseTask.registry["function_task"] is not None


@pytest.mark.unit
class TestFunctionTask:
    """
    Test cases for FunctionTask structure and creation.
    """

    def test_function_task_inherits_from_horus_task(self) -> None:
        """
        Test that FunctionTask properly inherits from HorusTask.
        """
        assert issubclass(FunctionTask, HorusTask)

    def test_function_task_kind_is_function_task(self) -> None:
        """
        Test that FunctionTask has the correct kind field.
        """
        task = FunctionTask(
            name="test_task",
            runtime=PythonFunctionRuntime(func=lambda: None),
        )

        assert task.kind == "function_task"

    def test_function_task_default_executor_is_python_function(self) -> None:
        """
        Test that FunctionTask defaults to PythonFunctionExecutor.
        """
        task = FunctionTask(
            name="test_task",
            runtime=PythonFunctionRuntime(func=lambda: None),
        )

        assert isinstance(task.executor, PythonFunctionExecutor)

    def test_function_task_creation_with_minimal_fields(self) -> None:
        """
        Test that FunctionTask can be created with minimal required fields.
        """
        task = FunctionTask(
            name="test_task",
            runtime=PythonFunctionRuntime(func=lambda: None),
        )

        assert task.name == "test_task"
        assert not task.inputs
        assert not task.outputs


@pytest.mark.unit
class TestFunctionTaskDecorator:
    """
    Test cases for the FunctionTask.task() decorator factory.
    """

    def test_decorator_adds_task_to_workflow(self) -> None:
        """
        Test that @FunctionTask.task(wf) registers the task in the workflow.
        """
        wf = HorusWorkflow(name="test_wf")

        @FunctionTask.task(wf)
        def my_func() -> None:
            pass

        assert "my_func" in wf.tasks

    def test_decorator_uses_function_name_as_task_name(self) -> None:
        """
        Test that the task name defaults to the decorated function's name.
        """
        wf = HorusWorkflow(name="test_wf")

        @FunctionTask.task(wf)
        def some_step() -> None:
            pass

        assert wf.tasks["some_step"].name == "some_step"

    def test_decorator_accepts_custom_name(self) -> None:
        """
        Test that a custom name can be provided to override the function name.
        """
        wf = HorusWorkflow(name="test_wf")

        @FunctionTask.task(wf, name="custom_name")
        def my_func() -> None:
            pass

        assert "custom_name" in wf.tasks
        assert wf.tasks["custom_name"].name == "custom_name"

    def test_decorator_returns_function_task_instance(self) -> None:
        """
        Test that the decorator returns a FunctionTask instance.
        """
        wf = HorusWorkflow(name="test_wf")

        @FunctionTask.task(wf)
        def my_func() -> None:
            pass

        assert isinstance(my_func, FunctionTask)

    def test_decorator_stores_callable_in_runtime(self) -> None:
        """
        Test that the decorated function is stored in the runtime.
        """
        wf = HorusWorkflow(name="test_wf")

        def my_func() -> None:
            pass

        task = FunctionTask.task(wf)(my_func)

        assert task.runtime.func is my_func

    def test_decorator_sets_task_id_matching_workflow_key(self) -> None:
        """
        Test that decorator-registered tasks have a task_id equal to their
        workflow key (i.e. task.task_id == task.name). This guards against
        the regression where t.task_id was not explicitly assigned, leaving
        it as the default UUID instead of the function name.
        """
        wf = HorusWorkflow(name="test_wf")

        @FunctionTask.task(wf)
        def my_step() -> None:
            pass

        task = wf.tasks["my_step"]
        assert task.task_id == task.name

    def test_decorator_custom_name_sets_task_id_matching_workflow_key(
        self,
    ) -> None:
        """
        Test that a custom-named decorator-registered task also has task_id
        equal to the custom name, not the underlying function name.
        """
        wf = HorusWorkflow(name="test_wf")

        @FunctionTask.task(wf, name="custom_step")
        def my_func() -> None:
            pass

        task = wf.tasks["custom_step"]
        assert task.task_id == "custom_step"

    async def test_decorator_raises_for_unknown_parameter_name(self) -> None:
        """
        Declared function parameters must be injectable from task context.
        """
        wf = HorusWorkflow(name="test_wf")
        with pytest.raises(ValueError, match="wrong_name"):
            tmp_file = Path("/tmp/test.txt")
            tmp_file.write_text("test")

            @FunctionTask.task(
                wf, inputs={"data": FileArtifact(path=tmp_file)}
            )
            def my_func(wrong_name: FileArtifact) -> None:
                pass

            # Try to run the task to trigger the setup_runtime logic that
            # checks parameter names.
            await wf.tasks["my_func"].run()


@pytest.mark.unit
class TestFunctionTaskExecution:
    """
    Test cases for FunctionTask execution.
    """

    async def test_run_calls_the_function(self) -> None:
        """
        Test that run() invokes the wrapped Python function.
        """
        called: list[bool] = []

        task = FunctionTask(
            name="test_task",
            runtime=PythonFunctionRuntime(func=lambda: called.append(True)),
        )

        await task.run()

        assert called == [True]

    async def test_run_increments_runs_count(self) -> None:
        """
        Test that run() increments the runs counter.
        """
        task = FunctionTask(
            name="test_task",
            runtime=PythonFunctionRuntime(func=lambda: None),
        )

        assert task.runs == 0
        await task.run()
        assert task.runs == 1

    async def test_reset_clears_runs_count(self) -> None:
        """
        Test that reset() sets the runs counter back to zero.
        """
        task = FunctionTask(
            name="test_task",
            runtime=PythonFunctionRuntime(func=lambda: None),
        )

        await task.run()
        assert task.runs == 1

        task.reset()
        assert task.runs == 0

    async def test_run_awaits_async_function(self) -> None:
        """
        Test that run() awaits wrapped async Python functions.
        """
        called: list[str] = []

        async def async_func() -> None:
            called.append("done")

        task = FunctionTask(
            name="test_async_task",
            runtime=PythonFunctionRuntime(func=async_func),
        )

        await task.run()

        assert called == ["done"]

    async def test_run_passes_task_when_function_accepts_parameter(
        self,
    ) -> None:
        """
        Test that run() passes the task instance as the first function
        argument when the wrapped callable accepts one.
        """
        received_tasks: list[BaseTask] = []

        def task_aware_func(task: BaseTask) -> None:
            received_tasks.append(task)

        task = FunctionTask(
            name="test_task",
            runtime=PythonFunctionRuntime(func=task_aware_func),
        )

        await task.run()

        assert received_tasks == [task]

    async def test_run_passes_declared_artifacts_by_name(
        self,
        tmp_path: Path,
    ) -> None:
        """
        Declared inputs and outputs should be injected by key name.
        """
        input_artifact = FileArtifact(
            id="input",
            path=tmp_path / "input.txt",
        )
        output_artifact = FileArtifact(
            id="output",
            path=tmp_path / "output.txt",
        )
        input_artifact.path.write_text("hello")

        def artifact_aware_func(
            input_file: FileArtifact,
            output_file: FileArtifact,
        ) -> None:
            output_file.path.write_text(input_file.path.read_text().upper())

        task = FunctionTask(
            name="artifact_task",
            runtime=PythonFunctionRuntime(func=artifact_aware_func),
            inputs={"input_file": input_artifact},
            outputs={"output_file": output_artifact},
        )

        await task.run()

        assert output_artifact.path.read_text() == "HELLO"
