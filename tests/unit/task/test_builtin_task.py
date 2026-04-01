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
Unit tests for HorusTask builtin task.
"""

from unittest.mock import patch

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.exceptions import ArtifactDoesNotExistError
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.exceptions import TaskExecutionError
from tests.conftest import MakeTaskType


@pytest.fixture(autouse=True)
def horus_context() -> HorusContext:
    """
    Fixture to provide a HorusContext for testing.
    """
    return HorusContext.boot()


@pytest.mark.unit
class TestInitRegistry:
    """
    Test that the builtin horus tasks are properly registered.
    """

    def test_init_registry_scans_builtin_tasks(self) -> None:
        """
        Test that init_registry properly scans the horus.tasks module.
        """
        assert "horus_task" in BaseTask.registry
        assert BaseTask.registry["horus_task"] is not None


@pytest.mark.unit
class TestTaskRegistry:
    """
    Test cases for task registry functionality.
    """

    def test_task_union_can_validate_horus_task(self) -> None:
        """
        Test TaskUnion can validate HorusTask data.
        """
        data = {
            "name": "test_task",
            "kind": "horus_task",
            "executor": {"kind": "shell"},
            "runtime": {"kind": "command", "command": "echo 'Hello World'"},
        }

        class TestModel(BaseModel):
            task: BaseTask

        result = TestModel.model_validate({"task": data})

        assert isinstance(result.task, HorusTask)
        assert result.task.kind == "horus_task"

    def test_task_registry_invalid_kind_handling(self) -> None:
        """
        Test that the task registry properly handles invalid kinds.
        """
        data = {
            "kind": "invalid_task_kind",
            "executor": {"kind": "shell"},
            "runtime": {"kind": "command", "command": "echo 'Hello World'"},
        }

        class TestModel(BaseModel):
            task: BaseTask

        with pytest.raises(ValidationError):
            TestModel.model_validate({"task": data})


@pytest.mark.unit
class TestHorusTask:
    """
    Test cases for HorusTask functionality.
    """

    def test_horus_task_inherits_from_base(self) -> None:
        """
        Test that HorusTask properly inherits from BaseTask.
        """
        assert issubclass(HorusTask, BaseTask)

    def test_horus_task_kind_is_horus_task(self) -> None:
        """
        Test that HorusTask has the correct kind field.
        """
        task = HorusTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo 'Hello World'"),
        )

        assert task.kind == "horus_task"

    def test_horus_task_run_method_exists(self) -> None:
        """
        Test that HorusTask implements the run method.
        """
        task = HorusTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo 'Hello World'"),
        )

        # Should not raise NotImplementedError
        assert callable(task.run)

    def test_horus_task_creation_with_minimal_fields(self) -> None:
        """
        Test that HorusTask can be created with minimal required fields.
        """
        task = HorusTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo 'Hello World'"),
        )

        assert task.kind == "horus_task"
        assert not task.inputs
        assert not task.outputs
        assert not task.variables

    def test_horus_task_creation_with_all_fields(self) -> None:
        """
        Test that HorusTask can be created with all fields specified.
        """
        input_artifact = FileArtifact(uri="input.txt")
        output_artifact = FileArtifact(uri="output.txt")

        task = HorusTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo 'Hello World'"),
            inputs={"input1": input_artifact},
            outputs={"output1": output_artifact},
            variables={"var1": "value1"},
        )

        assert task.kind == "horus_task"
        assert "input1" in task.inputs
        assert "output1" in task.outputs
        assert task.variables["var1"] == "value1"


@pytest.mark.unit
class TestHorusTaskExecution:
    """
    Test cases for HorusTask execution functionality.
    """

    async def test_horus_task_run_checks_input_artifacts_exist(self) -> None:
        """
        Test that HorusTask.run() checks if input artifacts exist.
        """
        # Use a path that definitely doesn't exist
        input_artifact = FileArtifact(
            uri="/definitely/nonexistent/path/file.txt"
        )

        task = HorusTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo 'Hello World'"),
            inputs={"input1": input_artifact},
        )

        with pytest.raises(ArtifactDoesNotExistError):
            await task.run()

    async def test_horus_task_run_executes_via_executor(self) -> None:
        """
        Test that HorusTask.run() executes the task via the executor.
        """
        task = HorusTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo 'Hello World'"),
            inputs={},  # No inputs to avoid file existence issues
        )

        # Mock subprocess.run to return success
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            # Should not raise an exception
            await task.run()

            # Verify that subprocess.run was called
            mock_run.assert_called_once()

    async def test_horus_task_run_raises_error_on_execution_failure(
        self,
    ) -> None:
        """
        Test that HorusTask.run() raises TaskExecutionError when executor
        returns non-zero.
        """
        task = HorusTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo 'Hello World'"),
        )

        # Mock subprocess.run to return failure
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1  # Non-zero return code

            with pytest.raises(TaskExecutionError):
                await task.run()

    async def test_horus_task_run_with_no_inputs(self) -> None:
        """
        Test that HorusTask.run() works correctly with no inputs.
        """
        task = HorusTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo 'Hello World'"),
            inputs={},  # No inputs
        )

        # Mock subprocess.run to return success
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            # Should not raise an exception
            await task.run()

            # Verify that subprocess.run was called
            mock_run.assert_called_once()

    async def test_horus_task_run_with_multiple_inputs_one_missing(
        self,
    ) -> None:
        """
        Test that HorusTask.run() stops on first missing artifact.
        """
        # Use paths that definitely don't exist
        input_artifact1 = FileArtifact(
            uri="/definitely/nonexistent/path/file1.txt"
        )
        input_artifact2 = FileArtifact(
            uri="/definitely/nonexistent/path/file2.txt"
        )

        task = HorusTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo 'Hello World'"),
            inputs={"input1": input_artifact1, "input2": input_artifact2},
        )

        # Should raise error when processing the inputs
        with pytest.raises(ArtifactDoesNotExistError):
            await task.run()

    async def test_horus_task_increases_runs_count(
        self, make_shell_task: MakeTaskType
    ) -> None:
        """
        Test that HorusTask.run() increases the runs count.
        """
        task = make_shell_task(cmd="echo 'Hello World'")

        initial_runs = task.runs

        # Mock subprocess.run to return success
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            await task.run()

            assert task.runs == initial_runs + 1

    async def test_horus_task_resets_runs_on_reset(
        self, make_shell_task: MakeTaskType
    ) -> None:
        """
        Test that HorusTask.reset() resets the runs count.
        """
        task = make_shell_task(cmd="echo 'Hello World'")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            # Simulate running the task a few times
            await task.run()

        assert task.runs == 1

        # Reset the task
        task.reset()

        assert task.runs == 0
