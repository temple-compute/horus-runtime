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

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.executor.python_exec import PythonExecExecutor
from horus_builtin.executor.python_fn import PythonFunctionExecutor
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.runtime.python_string import PythonCodeStringRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.context import HorusContext
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.settings import runtime_settings
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

    async def test_execute_successful_command(
        self,
        make_shell_task: MakeTaskType,
        horus_context: HorusContext,
    ) -> None:
        """
        Test executing a successful command injects SIDE_ARTIFACTS_DIR env var
        via the target channel's run_command.
        """
        del horus_context

        hello_world_task = make_shell_task("echo 'Hello World'")

        # Mock at the channel boundary: LocalTarget.run_command.
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.wait = AsyncMock(return_value=0)

        with patch.object(
            LocalTarget,
            "run_command",
            new=AsyncMock(return_value=mock_proc),
        ) as mock_run_command:
            executor = ShellExecutor()
            await executor.execute(hello_world_task)

            mock_run_command.assert_called_once()
            __, kwargs = mock_run_command.call_args
            # The executor injects the per-task side-artifacts directory into
            # the subprocess environment via the channel's env parameter.
            assert runtime_settings.SIDE_ARTIFACTS_DIR_ENV in kwargs["env"]
            assert kwargs["env"][
                runtime_settings.SIDE_ARTIFACTS_DIR_ENV
            ].endswith("side-artifacts")


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


@pytest.mark.unit
class TestPythonExecExecutor:
    """
    Test that the in-process Python executor runs in the task working dir.
    """

    async def test_exec_runs_in_task_working_dir(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Relative paths in exec'd code resolve to ``task.working_dir`` (issue
        #82), and the process cwd is restored afterwards.
        """
        del horus_context

        task = HorusTask(
            name="py_task",
            id="py_task_id",
            inputs=[],
            outputs=[],
            runtime=PythonCodeStringRuntime(
                code="from pathlib import Path; "
                "Path('out.txt').write_text('ok')"
            ),
            executor=PythonExecExecutor(),
            target=LocalTarget(working_directory=tmp_path.as_posix()),
        )

        cwd_before = os.getcwd()
        await task.executor.execute(task)

        assert (Path(task.working_dir) / "out.txt").read_text() == "ok"
        assert os.getcwd() == cwd_before


@pytest.mark.unit
class TestPythonFunctionExecutor:
    """
    Test that the in-process Python function executor runs in the task
    working dir (issue #82), for both sync and async functions.
    """

    @pytest.mark.parametrize("use_async", [False, True])
    async def test_function_runs_in_task_working_dir(
        self, use_async: bool, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Relative paths in the wrapped function resolve to ``task.working_dir``,
        and the process cwd is restored afterwards.
        """
        del horus_context

        def sync_fn() -> None:
            Path("out.txt").write_text("ok")

        async def async_fn() -> None:
            Path("out.txt").write_text("ok")

        task = HorusTask(
            name="fn_task",
            id="fn_task_id",
            inputs=[],
            outputs=[],
            runtime=PythonFunctionRuntime(
                func=async_fn if use_async else sync_fn
            ),
            executor=PythonFunctionExecutor(),
            target=LocalTarget(working_directory=tmp_path.as_posix()),
        )

        cwd_before = os.getcwd()
        await task.executor.execute(task)

        assert (Path(task.working_dir) / "out.txt").read_text() == "ok"
        assert os.getcwd() == cwd_before
