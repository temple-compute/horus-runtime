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
Unit tests for LocalExecutor and related builtin executors
"""

import subprocess
from unittest.mock import Mock, patch

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.executors.local import LocalExecutor
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.registry.auto_registry import init_registry


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
        assert "local" in BaseExecutor.registry

        assert BaseExecutor.registry["local"] is LocalExecutor

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
        Test that ExecutorUnion can validate LocalExecutor data
        """
        data = {"kind": "local"}

        from horus_runtime.core.registry.executor_registry import ExecutorUnion

        class TestModel(BaseModel):
            executor: ExecutorUnion

        # This should work with the discriminated union
        result = TestModel.model_validate({"executor": data})

        # Check LocalExecutor
        assert isinstance(result.executor, LocalExecutor)
        assert result.executor.kind == "local"

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
        assert "local" in BaseExecutor.registry


@pytest.mark.unit
class TestLocalExecutor:
    """
    Test cases for LocalExecutor class
    """

    def test_local_executor_inherits_from_base(self) -> None:
        """
        Test that LocalExecutor inherits from BaseExecutor
        """
        assert issubclass(LocalExecutor, BaseExecutor)

    def test_local_executor_kind_is_local(self) -> None:
        """
        Test that LocalExecutor has correct kind value
        """
        executor = LocalExecutor()
        assert executor.kind == "local"

    def test_local_executor_deserialization(self) -> None:
        """
        Test that LocalExecutor can be deserialized
        """
        data = {"kind": "local"}
        executor = LocalExecutor.model_validate(data)

        assert executor.kind == "local"

    @patch("subprocess.run")
    def test_execute_successful_command(self, mock_run: Mock) -> None:
        """
        Test executing a successful command returns correct exit code
        """
        # Mock subprocess.run to return successful execution
        mock_result = Mock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        executor = LocalExecutor()
        result = executor.execute("echo 'Hello World'")

        assert result == 0
        mock_run.assert_called_once_with(
            "echo 'Hello World'", shell=True, check=True, text=True
        )


@pytest.mark.unit
class TestLocalExecutorIntegration:
    """
    Integration tests for LocalExecutor with real subprocess calls
    """

    def test_execute_real_successful_command(self) -> None:
        """
        Test executing a real successful command (echo)
        """
        executor = LocalExecutor()

        # Use a simple, cross-platform command that should always work
        result = executor.execute("echo test")

        # Echo should return 0 on success
        assert result == 0

    def test_execute_real_failed_command(self) -> None:
        """
        Test executing a real command that fails
        """
        executor = LocalExecutor()

        # Use a command that should fail (exit with non-zero code)
        with pytest.raises(subprocess.CalledProcessError):
            # This command should fail since "nonexistent_command_xyz" doesn't
            # exist
            executor.execute("nonexistent_command_xyz_that_should_not_exist")

    def test_execute_real_command_with_exit_code(self) -> None:
        """
        Test executing a real command with specific exit code
        """
        executor = LocalExecutor()

        # Use 'true' command which should always exit with 0
        result = executor.execute("true")
        assert result == 0
