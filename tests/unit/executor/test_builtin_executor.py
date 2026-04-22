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
Unit tests for ShellExecutor and related builtin executors.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.executor.shell import ShellExecutor
from horus_runtime.context import HorusContext
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.task.exceptions import TaskExecutionError
from tests.conftest import MakeTaskType


@pytest.mark.unit
class TestInitRegistry:
    """
    Test that the builtin horus executors are properly registered.
    """

    def test_init_registry_scans_builtin_executors(self) -> None:
        """
        Test that init_registry scans the core executors package.
        """
        # Should have scanned the core executors package
        assert "shell" in BaseExecutor.registry

        assert BaseExecutor.registry["shell"] is ShellExecutor


@pytest.mark.unit
class TestExecutorRegistry:
    """
    Test cases for ExecutorUnion type alias.
    """

    def test_executor_union_can_validate_union_executor(self) -> None:
        """
        Test that ExecutorUnion can validate ShellExecutor data.
        """
        data = {"kind": "shell"}

        class TestModel(BaseModel):
            executor: BaseExecutor

        # This should work with the discriminated union
        result = TestModel.model_validate({"executor": data})

        # Check ShellExecutor
        assert isinstance(result.executor, ShellExecutor)
        assert result.executor.kind == "shell"

    def test_executor_registry_invalid_kind_handling(self) -> None:
        """
        Test handling of invalid kind values.
        """
        invalid_data = {"kind": "invalid_type"}

        class TestModel(BaseModel):
            executor: BaseExecutor

        # Should raise validation error for unknown kind
        with pytest.raises(ValidationError):
            # Try to validate with a known executor type - should fail
            # because kind doesn't match
            TestModel.model_validate({"executor": invalid_data})


@pytest.mark.unit
class TestShellExecutor:
    """
    Test cases for ShellExecutor class.
    """

    def test_shell_executor_inherits_from_base(self) -> None:
        """
        Test that ShellExecutor inherits from BaseExecutor.
        """
        assert issubclass(ShellExecutor, BaseExecutor)

    def test_shell_executor_kind_is_shell(self) -> None:
        """
        Test that ShellExecutor has correct kind value.
        """
        executor = ShellExecutor()
        assert executor.kind == "shell"

    @patch("asyncio.create_subprocess_shell")
    async def test_execute_successful_command(
        self,
        mock_run: AsyncMock,
        make_shell_task: MakeTaskType,
        horus_context: HorusContext,
    ) -> None:
        """
        Test executing a successful command returns correct exit code.
        """
        del horus_context
        # Mock asyncio.create_subprocess_shell to return successful execution
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_run.return_value = mock_process

        hello_world_task = make_shell_task("echo 'Hello World'")

        executor = ShellExecutor()
        await executor.execute(hello_world_task)

        mock_run.assert_called_once_with(
            "echo 'Hello World'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )


@pytest.mark.unit
class TestShellExecutorIntegration:
    """
    Integration tests for ShellExecutor with real subprocess calls.
    """

    async def test_execute_real_successful_command(
        self, make_shell_task: MakeTaskType, horus_context: HorusContext
    ) -> None:
        """
        Test executing a real successful command (echo).
        """
        del horus_context

        executor = ShellExecutor()

        # Use a simple, cross-platform command that should always work
        hello_world_task = make_shell_task("echo 'Hello World'")
        await executor.execute(hello_world_task)

    async def test_execute_real_failed_command(
        self, make_shell_task: MakeTaskType, horus_context: HorusContext
    ) -> None:
        """
        Test executing a real command that fails.
        """
        del horus_context
        executor = ShellExecutor()

        task = make_shell_task("nonexistent_command_xyz_that_should_not_exist")

        # Command should fail and raise TaskExecutionError
        with pytest.raises(TaskExecutionError):
            await executor.execute(task)

    async def test_execute_real_command_with_exit_code(
        self, make_shell_task: MakeTaskType, horus_context: HorusContext
    ) -> None:
        """
        Test executing a real command with specific exit code.
        """
        del horus_context
        executor = ShellExecutor()

        # Use 'true' command which should always exit with 0
        task = make_shell_task("true")
        await executor.execute(task)
