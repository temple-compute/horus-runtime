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

# pylint: disable=import-outside-toplevel, redefined-outer-name, unused-import
# pylint: disable=missing-class-docstring, missing-function-docstring
# pylint: disable=reimported
"""
Unit tests for ShellExecutor and related builtin executors
"""

from typing import Callable
from unittest.mock import Mock, patch

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.executors.shell import ShellExecutor
from horus_builtin.runtimes.command import CommandRuntime
from horus_builtin.tasks.horus_task import HorusTask
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.registry.auto_registry import init_registry

MakeTaskType = Callable[[str], HorusTask]


@pytest.fixture
def make_task() -> MakeTaskType:

    def _make_task(cmd: str = "echo 'Hello World'") -> HorusTask:

        runtime = CommandRuntime(command=cmd)

        return HorusTask(
            inputs={}, outputs={}, runtime=runtime, executor=ShellExecutor()
        )

    return _make_task


@pytest.mark.unit
class TestInitRegistry:
    """
    Test that the builtin horus executors are properly registered
    """

    def test_init_registry_scans_builtin_executors(self) -> None:
        """
        Test that init_registry scans the core executors package
        """

        init_registry(BaseExecutor, "horus.executors")

        # Should have scanned the core executors package
        assert "shell" in BaseExecutor.registry

        assert BaseExecutor.registry["shell"] is ShellExecutor

    def test_init_registry_returns_union_type(self) -> None:
        """
        Test that init_registry returns a proper Union type annotation
        """
        registry_union = init_registry(BaseExecutor, "horus.executors")

        # Result should be a type annotation that can be used with Pydantic
        assert registry_union is not None


@pytest.mark.unit
class TestExecutorRegistry:
    """
    Test cases for ExecutorUnion type alias
    """

    def test_executor_union_is_defined(self) -> None:
        """
        Test that ExecutorUnion type alias is properly defined
        """
        from horus_runtime.core.registry.executor_registry import ExecutorUnion

        assert ExecutorUnion is not None

    def test_executor_union_can_validate_union_executor(self) -> None:
        """
        Test that ExecutorUnion can validate ShellExecutor data
        """
        data = {"kind": "shell"}

        from horus_runtime.core.registry.executor_registry import ExecutorUnion

        class TestModel(BaseModel):
            executor: ExecutorUnion

        # This should work with the discriminated union
        result = TestModel.model_validate({"executor": data})

        # Check ShellExecutor
        assert isinstance(result.executor, ShellExecutor)
        assert result.executor.kind == "shell"

    def test_executor_registry_invalid_kind_handling(self) -> None:
        """
        Test handling of invalid kind values
        """

        from horus_runtime.core.registry.executor_registry import ExecutorUnion

        invalid_data = {"kind": "invalid_type"}

        class TestModel(BaseModel):
            executor: ExecutorUnion

        # Should raise validation error for unknown kind
        with pytest.raises(ValidationError):
            # Try to validate with a known executor type - should fail
            # because kind doesn't match
            TestModel.model_validate({"executor": invalid_data})


@pytest.mark.integration
class TestExecutorRegistryIntegration:
    """
    Integration tests for the full executor registry system
    """

    def test_registry_contains_expected_executors(self) -> None:
        """
        Test that the registry contains the expected executor types
        """
        # Access the registry from BaseExecutor after scanning
        # noqa: F401
        from horus_runtime.core.executor.base import BaseExecutor
        from horus_runtime.core.registry.executor_registry import (  # noqa: F401,E501
            ExecutorUnion,
        )

        # Registry should contain local executor
        assert hasattr(BaseExecutor, "registry")
        assert "shell" in BaseExecutor.registry


@pytest.mark.unit
class TestShellExecutor:
    """
    Test cases for ShellExecutor class
    """

    def test_shell_executor_inherits_from_base(self) -> None:
        """
        Test that ShellExecutor inherits from BaseExecutor
        """
        assert issubclass(ShellExecutor, BaseExecutor)

    def test_shell_executor_kind_is_shell(self) -> None:
        """
        Test that ShellExecutor has correct kind value
        """
        executor = ShellExecutor()
        assert executor.kind == "shell"

    def test_shell_executor_deserialization(self) -> None:
        """
        Test that ShellExecutor can be deserialized
        """
        data = {"kind": "shell"}
        executor = ShellExecutor.model_validate(data)

        assert executor.kind == "shell"

    @patch("subprocess.run")
    def test_execute_successful_command(
        self, mock_run: Mock, make_task: MakeTaskType
    ) -> None:
        """
        Test executing a successful command returns correct exit code
        """
        # Mock subprocess.run to return successful execution
        mock_result = Mock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        hello_world_task = make_task("echo 'Hello World'")

        executor = ShellExecutor()
        result = executor.execute(hello_world_task)

        assert result == 0
        mock_run.assert_called_once_with(
            "echo 'Hello World'", shell=True, check=False, text=True
        )


@pytest.mark.unit
class TestShellExecutorIntegration:
    """
    Integration tests for ShellExecutor with real subprocess calls
    """

    def test_execute_real_successful_command(
        self, make_task: MakeTaskType
    ) -> None:
        """
        Test executing a real successful command (echo)
        """
        executor = ShellExecutor()

        # Use a simple, cross-platform command that should always work
        hello_world_task = make_task("echo 'Hello World'")
        result = executor.execute(hello_world_task)

        # Echo should return 0 on success
        assert result == 0

    def test_execute_real_failed_command(
        self, make_task: MakeTaskType
    ) -> None:
        """
        Test executing a real command that fails
        """
        executor = ShellExecutor()

        task = make_task("nonexistent_command_xyz_that_should_not_exist")

        # Use a command that should fail (exit with non-zero code)
        result = executor.execute(task)
        assert result != 0

    def test_execute_real_command_with_exit_code(
        self, make_task: MakeTaskType
    ) -> None:
        """
        Test executing a real command with specific exit code
        """
        executor = ShellExecutor()

        # Use 'true' command which should always exit with 0
        task = make_task("true")
        result = executor.execute(task)
        assert result == 0
