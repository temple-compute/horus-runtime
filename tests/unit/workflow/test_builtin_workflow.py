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
Unit tests for the Workflow class.
"""

import textwrap
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, Mock, patch

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.task.exceptions import (
    TaskExecutionError,
    TaskMissingIdError,
)
from tests.conftest import MakeTaskType, MakeWorkflowFileType


@pytest.mark.unit
class TestWorkflowConstruction:
    """
    Tests for constructing Workflow instances.
    """

    def test_basic_construction(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Test that a workflow can be constructed from a valid YAML file.
        """
        wf_content = textwrap.dedent("""\
            name: test_workflow
            kind: horus_workflow
            tasks:
              t1:
                id: test_task_id
                name: task1
                kind: horus_task
                runtime:
                  kind: command
                  command: "echo hello"
                executor:
                  kind: shell
            """)

        wf_file = make_workflow_file(tmp_path, wf_content)
        wf = HorusWorkflow.from_yaml(wf_file)

        assert wf.name == "test_workflow"
        assert isinstance(wf, HorusWorkflow)
        assert wf.kind == "horus_workflow"
        assert "t1" in wf.tasks

    def test_tasks_preserve_order(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Test that tasks are loaded in the order they are defined in the YAML
        file.
        """
        wf_content = textwrap.dedent("""\
            name: ordered
            kind: horus_workflow
            tasks:
              a:
                id: task_a_id
                name: A
                kind: horus_task
                runtime:
                  kind: command
                  command: "echo A"
                executor:
                  kind: shell
              b:
                id: task_b_id
                name: B
                kind: horus_task
                runtime:
                  kind: command
                  command: "echo B"
                executor:
                  kind: shell
              c:
                id: task_c_id
                name: C
                kind: horus_task
                runtime:
                  kind: command
                  command: "echo C"
                executor:
                  kind: shell
            """)
        wf_file = make_workflow_file(tmp_path, wf_content)
        wf = HorusWorkflow.from_yaml(wf_file)
        assert list(wf.tasks.keys()) == ["a", "b", "c"]

    def test_empty_tasks(self) -> None:
        """
        Test that a workflow can be created with an empty tasks dictionary.
        """
        wf = HorusWorkflow(name="empty", tasks={})
        assert wf.tasks == {}


@pytest.mark.unit
class TestWorkflowRun:
    """
    Tests for the run method of the Workflow class.
    """

    async def test_run_executes_task_without_outputs(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Test that a workflow with a single task and no outputs executes the
        task.
        """
        wf_contents = textwrap.dedent("""\
        name: run_test
        kind: horus_workflow
        tasks:
            test_task_id:
                id: test_task_id
                name: Task
                kind: horus_task
                runtime:
                    kind: command
                    command: "echo test"
                executor:
                    kind: shell
        """)

        wf_file = make_workflow_file(tmp_path, wf_contents)
        wf = HorusWorkflow.from_yaml(wf_file)

        with patch.object(HorusTask, "run") as mock_run:
            await wf.run()

        mock_run.assert_awaited_once()

    async def test_run_skips_task_when_all_outputs_exist(
        self,
        tmp_path: Path,
        make_workflow_file: MakeWorkflowFileType,
        horus_context: HorusContext,
    ) -> None:
        """
        Test that a task is skipped if all its outputs already exist.
        """
        del horus_context
        wf_contents = textwrap.dedent("""\
            name: skip_test
            kind: horus_workflow
            tasks:
                test_task_id:
                    id: test_task_id
                    name: Task
                    kind: horus_task
                    skip_if_complete: True
                    runtime:
                        kind: command
                        command: "echo test"
                    executor:
                        kind: shell
                    outputs:
                        output_file:
                            id: output_file
                            kind: file
                            path: /tmp/some_file.txt
            """)
        wf_file = make_workflow_file(tmp_path, wf_contents)
        wf = HorusWorkflow.from_yaml(wf_file)

        with (
            patch.object(FileArtifact, "exists", return_value=True),
            patch.object(HorusTask, "_run") as mock_run,
        ):
            await wf.run()

        mock_run.assert_not_called()

    @patch("asyncio.create_subprocess_shell")
    async def test_run_executes_tasks_in_order(
        self, mock_run: AsyncMock, make_shell_task: MakeTaskType
    ) -> None:
        """
        Tasks should be executed in the order they are defined in the workflow.
        """
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_run.return_value = mock_process

        task_a = make_shell_task(cmd="echo A")
        task_b = make_shell_task(cmd="echo B")
        wf = HorusWorkflow(name="order_test", tasks={"a": task_a, "b": task_b})
        await wf.run()

        # Two tasks, two calls
        function_calls = 2

        assert mock_run.call_count == function_calls

        calls = [call.args[0] for call in mock_run.call_args_list]
        assert calls == ["echo A", "echo B"]

    async def test_run_stops_on_first_failure(
        self, make_shell_task: MakeTaskType
    ) -> None:
        """
        If a task fails, the workflow should stop and not
        execute subsequent tasks.
        """

        class TaskWithFailure(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                await super()._run()  # Here it calls +1 run count
                raise TaskExecutionError("fail")

        task_a = TaskWithFailure(
            id="test_task_id",
            name="test_task",
            inputs={},
            outputs={},
            runtime=CommandRuntime(command="echo A"),
            executor=ShellExecutor(),
            target=LocalTarget(),
        )
        task_b = make_shell_task(cmd="echo B")

        wf = HorusWorkflow(name="stop_test", tasks={"a": task_a, "b": task_b})
        with pytest.raises(TaskExecutionError):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=0)
                await wf.run()

        assert task_a.runs == 1
        assert task_b.runs == 0

    async def test_run_empty_workflow(self) -> None:
        """
        Test that running an empty workflow completes without error.
        """
        # Empty flow
        wf = HorusWorkflow(name="empty", tasks={})

        # Should complete without error
        await wf.run()

    async def test_run_raises_when_task_has_no_id(
        self, make_shell_task: MakeTaskType, horus_context: HorusContext
    ) -> None:
        """
        Test that running a workflow raises TaskMissingIdError when a task has
        no task_id. This guards against tasks added after workflow construction
        (e.g. via a decorator) without task_id being explicitly set.
        """
        del horus_context
        task = make_shell_task(cmd="echo test")

        wf = HorusWorkflow(
            name="missing_id",
            tasks={"orphan": task},
        )

        # Manually patch the task to remove the ID
        task.id = None  # type: ignore

        with (
            pytest.raises(TaskMissingIdError),
        ):
            await wf.run()
