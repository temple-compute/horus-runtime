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
Test configuration for pytest.
"""

from pathlib import Path
from typing import Protocol

import pytest

from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.registry.auto_registry import AutoRegistry


def pytest_configure(config: pytest.Config) -> None:
    """
    Configure pytest with custom markers.
    """
    config.addinivalue_line("markers", "unit: Unit tests")
    config.addinivalue_line("markers", "integration: Integration tests")
    config.addinivalue_line("markers", "slow: Slow running tests")


class MakeTaskType(Protocol):
    """
    Protocol for a factory function that creates HorusTask instances for
    testing.
    """

    def __call__(
        self,
        cmd: str = ...,
        inputs: dict[str, "BaseArtifact"] | None = None,
        task_class: type["HorusTask"] = ...,
    ) -> "HorusTask":
        """
        Create a HorusTask instance with the given command, inputs,
        and task class.
        """
        ...


@pytest.fixture
def make_shell_task() -> MakeTaskType:
    """
    Fixture to create HorusTask instances with CommandRuntime for testing.
    """

    # Factory function to create a HorusTask with a given command
    def _make_shell_task(
        cmd: str = "echo 'Hello World'",
        inputs: dict[str, "BaseArtifact"] | None = None,
        task_class: type["HorusTask"] = HorusTask,
    ) -> "HorusTask":

        runtime = CommandRuntime(command=cmd)

        return task_class(
            name="test_task",
            inputs=inputs or {},
            outputs={},
            runtime=runtime,
            executor=ShellExecutor(),
        )

    return _make_shell_task


class MakeWorkflowFileType(Protocol):
    """
    Protocol for a factory function that creates temporary workflow YAML files
    for testing.
    """

    def __call__(self, tmp_path: Path, content: str) -> Path:
        """
        Create a temporary workflow YAML file with the given content.
        """
        ...


@pytest.fixture
def make_workflow_file() -> MakeWorkflowFileType:
    """
    Fixture to create a temporary workflow YAML file for testing.
    """

    def _make_workflow_file(tmp_path: Path, content: str) -> Path:
        workflow_file = tmp_path / "workflow.yml"
        workflow_file.write_text(content)
        return workflow_file

    return _make_workflow_file


@pytest.fixture(scope="session", autouse=True)
def init_registry() -> None:
    """
    Load all built-in plugins once for the entire test session.
    """
    AutoRegistry.init_registry()
