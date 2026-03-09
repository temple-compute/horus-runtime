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
Unit tests for CommandRuntime builtin runtime.
"""

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.artifacts.file import FileArtifact
from horus_builtin.runtimes.command import CommandRuntime
from horus_runtime.core.runtime.base import BaseRuntime
from tests.conftest import MakeTaskType


@pytest.mark.unit
class TestInitRegistry:
    """
    Test that the builtin horus runtimes are properly registered.
    """

    def test_init_registry_scans_builtin_runtimes(self) -> None:
        """
        Test that init_registry properly scans the horus.runtimes module.
        """
        assert "command" in BaseRuntime.registry

        assert BaseRuntime.registry["command"] is not None


@pytest.mark.unit
class TestRuntimeRegistry:
    """
    Test cases for runtime registry functionality.
    """

    def test_runtime_union_can_validate_union_runtime(self) -> None:
        """
        Test RuntimeUnion can validate CommandRuntime data.
        """
        data = {
            "kind": "command",
            "command": "echo 'Hello World'",
        }

        class TestModel(BaseModel):
            runtime: BaseRuntime

        result = TestModel.model_validate({"runtime": data})

        assert isinstance(result.runtime, CommandRuntime)
        assert result.runtime.kind == "command"

    def test_runtime_registry_invalid_kind_handling(self) -> None:
        """
        Test that the runtime registry properly handles invalid kinds.
        """
        data = {
            "kind": "invalid_runtime_kind",
            "command": "echo 'Hello World'",
        }

        class TestModel(BaseModel):
            runtime: BaseRuntime

        with pytest.raises(ValidationError):
            TestModel.model_validate({"runtime": data})


@pytest.mark.unit
class TestCommandRuntime:
    """
    Test cases for CommandRuntime functionality.
    """

    def test_command_runtime_inherits_from_base(self) -> None:
        """
        Test that CommandRuntime properly inherits from BaseRuntime.
        """
        assert issubclass(CommandRuntime, BaseRuntime)

    def test_command_runtime_kind_is_command(self) -> None:
        """
        Test that CommandRuntime has the correct kind field.
        """
        runtime = CommandRuntime(command="echo 'Hello World'")

        assert runtime.kind == "command"

    def test_command_runtime_formats_command_with_inputs(
        self, make_task: MakeTaskType
    ) -> None:
        """
        Test that CommandRuntime properly formats commands with task inputs.
        """
        task = make_task(
            cmd="echo 'Input artifact path is {input1.path}'",
            inputs={"input1": FileArtifact(uri="test")},
        )

        formatted_cmd = task.runtime.format_runtime(task)

        assert "Input artifact path is" in formatted_cmd
        assert "{input1.path}" not in formatted_cmd

    def test_command_runtime_formats_command_with_task_variables(
        self, make_task: MakeTaskType
    ) -> None:
        """
        Test that CommandRuntime can access task variables in command
        formatting.
        """
        task = make_task("echo 'Task kind is {task.kind}'")

        formatted_cmd = task.runtime.format_runtime(task)

        assert "Task kind is horus_task" in formatted_cmd
