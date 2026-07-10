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
from unittest.mock import MagicMock, patch

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.python_fn import PythonFunctionExecutor
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.function import FunctionTask
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
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
            id="test_task",
            name="test_task",
            runtime=PythonFunctionRuntime(func=lambda: None),
        )

        assert task.kind == "function_task"

    def test_function_task_default_executor_is_python_function(self) -> None:
        """
        Test that FunctionTask defaults to PythonFunctionExecutor.
        """
        task = FunctionTask(
            id="test_task",
            name="test_task",
            runtime=PythonFunctionRuntime(func=lambda: None),
        )

        assert isinstance(task.executor, PythonFunctionExecutor)

    def test_function_task_creation_with_minimal_fields(self) -> None:
        """
        Test that FunctionTask can be created with minimal required fields.
        """
        task = FunctionTask(
            id="test_task",
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

        assert "my_func" in [t.id for t in wf.tasks]

    def test_decorator_uses_function_name_as_task_name(self) -> None:
        """
        Test that the task name defaults to the decorated function's name.
        """
        wf = HorusWorkflow(name="test_wf")

        @FunctionTask.task(wf)
        def some_step() -> None:
            pass

        task = wf.tasks[0]
        assert task.name == "some_step"

    def test_decorator_accepts_custom_name(self) -> None:
        """
        Test that a custom name can be provided to override the function name.
        """
        wf = HorusWorkflow(name="test_wf")

        @FunctionTask.task(wf, name="custom_name")
        def my_func() -> None:
            pass

        task = wf.tasks[0]
        assert task.name == "custom_name"
        # No explicit id was given, so it defaults to the function name.
        assert task.id == "my_func"

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

    def test_decorator_sets_task_id_matching_workflow_key(
        self,
    ) -> None:
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

        task = wf.tasks[0]
        assert task.name == "my_step"
        assert task.id == task.name

    def test_decorator_custom_name_sets_task_id_matching_workflow_key(
        self, horus_context: HorusContext
    ) -> None:
        """
        Test that a custom-named decorator-registered task also has task_id
        equal to the custom name, not the underlying function name.
        """
        del horus_context

        wf = HorusWorkflow(name="test_wf")

        @FunctionTask.task(wf, id="custom_step_id", name="custom_step")
        def my_func() -> None:
            pass

        task = wf.tasks[0]
        assert task.name == "custom_step"
        assert task.id == "custom_step_id"

    async def test_decorator_raises_for_unknown_parameter_name(
        self, tmp_path: Path
    ) -> None:
        """
        Declared function parameters must be injectable from task context.
        """
        wf = HorusWorkflow(name="test_wf")
        with pytest.raises(ValueError, match="wrong_name"):
            tmp_path_file = tmp_path / "data.txt"
            tmp_path_file.write_text("test")

            @FunctionTask.task(
                wf,
                inputs=[FileArtifact(id="data", path=tmp_path_file)],
            )
            def my_func(wrong_name: FileArtifact) -> None:
                pass

            # Try to run the task to trigger the setup_runtime logic that
            # checks parameter names.
            task = wf.tasks[0]
            await task.run()


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
            id="test_task",
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
            id="test_task",
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
            id="test_task",
            name="test_task",
            runtime=PythonFunctionRuntime(func=lambda: None),
        )

        await task.run()
        assert task.runs == 1

        await task.reset()
        assert task.runs == 0

    async def test_run_awaits_async_function(self) -> None:
        """
        Test that run() awaits wrapped async Python functions.
        """
        called: list[str] = []

        async def async_func() -> None:
            called.append("done")

        task = FunctionTask(
            id="test_async_task",
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
            id="test_task",
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
            id="input_file",
            path=tmp_path / "input.txt",
        )
        output_artifact = FileArtifact(
            id="output_file",
            path=tmp_path / "output.txt",
        )
        input_artifact.path.write_text("hello")

        def artifact_aware_func(
            input_file: FileArtifact,
            output_file: FileArtifact,
        ) -> None:
            output_file.path.write_text(input_file.path.read_text().upper())

        task = FunctionTask(
            id="artifact_task",
            name="artifact_task",
            runtime=PythonFunctionRuntime(func=artifact_aware_func),
            inputs=[input_artifact],
            outputs=[output_artifact],
        )

        await task.run()

        assert output_artifact.path.read_text() == "HELLO"


def _user_artifacts(task: BaseTask) -> list[BaseArtifact]:
    """side_artifacts minus the per-task log file the middleware auto-adds."""
    return [a for a in task.side_artifacts if a.id != f"{task.id}_logs"]


@pytest.mark.unit
class TestFunctionTaskSideArtifacts:
    """
    Test cases for side artifact capture via Python function return values.
    """

    async def test_sync_function_returning_single_artifact_captured(
        self,
        tmp_path: Path,
    ) -> None:
        """
        A sync function returning a single BaseArtifact populates
        task.side_artifacts with that artifact.
        """
        artifact = FileArtifact(id="out", path=tmp_path / "out.txt")

        task = FunctionTask(
            id="side_task",
            name="side_task",
            runtime=PythonFunctionRuntime(func=lambda: artifact),
        )

        await task.run()

        assert _user_artifacts(task) == [artifact]

    async def test_sync_function_returning_list_of_artifacts_captured(
        self,
        tmp_path: Path,
    ) -> None:
        """
        A sync function returning list[BaseArtifact] populates
        task.side_artifacts with all artifacts in order.
        """
        art1 = FileArtifact(id="art1", path=tmp_path / "a.txt")
        art2 = FileArtifact(id="art2", path=tmp_path / "b.txt")

        task = FunctionTask(
            id="side_task",
            name="side_task",
            runtime=PythonFunctionRuntime(func=lambda: [art1, art2]),
        )

        await task.run()

        assert _user_artifacts(task) == [art1, art2]

    async def test_async_function_returning_single_artifact_captured(
        self,
        tmp_path: Path,
    ) -> None:
        """
        An async function returning a single BaseArtifact is awaited and the
        artifact is captured on task.side_artifacts.
        """
        artifact = FileArtifact(id="async_out", path=tmp_path / "out.txt")

        async def async_func() -> FileArtifact:
            return artifact

        task = FunctionTask(
            id="async_side_task",
            name="async_side_task",
            runtime=PythonFunctionRuntime(func=async_func),
        )

        await task.run()

        assert _user_artifacts(task) == [artifact]

    async def test_async_function_returning_list_of_artifacts_captured(
        self,
        tmp_path: Path,
    ) -> None:
        """
        An async function returning list[BaseArtifact] is awaited and all
        artifacts are captured on task.side_artifacts.
        """
        art1 = FileArtifact(id="a1", path=tmp_path / "a.txt")
        art2 = FileArtifact(id="a2", path=tmp_path / "b.txt")

        async def async_func() -> list[BaseArtifact]:
            return [art1, art2]

        task = FunctionTask(
            id="async_list_task",
            name="async_list_task",
            runtime=PythonFunctionRuntime(func=async_func),
        )

        await task.run()

        assert _user_artifacts(task) == [art1, art2]

    async def test_none_return_leaves_side_artifacts_empty(self) -> None:
        """
        A function returning None leaves task.side_artifacts empty.
        """
        task = FunctionTask(
            id="none_task",
            name="none_task",
            runtime=PythonFunctionRuntime(func=lambda: None),
        )

        await task.run()

        assert _user_artifacts(task) == []

    @patch("horus_builtin.executor.python_fn.horus_logger")
    async def test_unexpected_return_type_logs_warning_and_skips(
        self,
        mock_logger: MagicMock,
    ) -> None:
        """
        A function returning an unexpected type (not BaseArtifact, list, or
        None) leaves side_artifacts empty and logs a warning.
        """
        task = FunctionTask(
            id="bad_return_task",
            name="bad_return_task",
            runtime=PythonFunctionRuntime(func=lambda: 42),  # type: ignore[return-value, arg-type]
        )

        await task.run()

        assert _user_artifacts(task) == []
        mock_logger.log.warning.assert_called_once()

    async def test_returned_and_filesystem_artifacts_are_both_captured(
        self,
        tmp_path: Path,
    ) -> None:
        """
        When a function both returns a BaseArtifact and writes a file to
        task.side_artifacts_dir, the returned artifact appears first (index 0)
        and the filesystem-collected artifact is appended after (index 1).
        """
        returned_artifact = FileArtifact(
            id="returned", path=tmp_path / "returned.txt"
        )

        def func(task: BaseTask) -> FileArtifact:
            (Path(task.side_artifacts_dir) / "fs.txt").write_text("from fs")
            return returned_artifact

        task = FunctionTask(
            id="merge_task",
            name="merge_task",
            runtime=PythonFunctionRuntime(func=func),
            target=LocalTarget(working_directory=tmp_path.as_posix()),
        )

        await task.run()

        # run() collects filesystem side artifacts itself (executor.execute's
        # finally), so the returned artifact stays first and the collected
        # fs.txt is appended after it.
        user = _user_artifacts(task)
        assert len(user) == 2
        assert user[0] is returned_artifact
        assert user[1].id == "merge_task_fs.txt"

    async def test_print_is_captured_in_task_log(self) -> None:
        """
        print() inside a task is forwarded to loguru by the log-file
        middleware, so it lands in the per-task ``.log`` side artifact.
        """
        task = FunctionTask(
            id="print_task",
            name="print_task",
            runtime=PythonFunctionRuntime(
                func=lambda: print("marker-xyz")  # noqa: T201
            ),
        )

        await task.run()

        log = next(a for a in task.side_artifacts if a.id == "print_task_logs")
        assert "marker-xyz" in Path(log.path).read_text()
