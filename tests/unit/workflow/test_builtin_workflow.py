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
Unit tests for the Workflow class
"""

import textwrap
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from horus_builtin.artifacts.file import FileArtifact
from horus_builtin.tasks.horus_task import HorusTask
from horus_builtin.workflows.horus_workflow import HorusWorkflow
from horus_runtime.core.task.exceptions import TaskExecutionError
from tests.conftest import MakeTaskType, MakeWorkflowFileType


@pytest.mark.unit
class TestWorkflowConstruction:
    def test_basic_construction(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:

        wf_content = textwrap.dedent("""\
            name: test_workflow
            kind: horus_workflow
            tasks:
              t1:
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
        wf_content = textwrap.dedent("""\
            name: ordered
            kind: horus_workflow
            tasks:
              a:
                name: A
                kind: horus_task
                runtime:
                  kind: command
                  command: "echo A"
                executor:
                  kind: shell
              b:
                name: B
                kind: horus_task
                runtime:
                  kind: command
                  command: "echo B"
                executor:
                  kind: shell
              c:
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
        wf = HorusWorkflow(name="empty", tasks={})
        assert wf.tasks == {}


@pytest.mark.unit
class TestWorkflowRun:
    def test_run_executes_task_without_outputs(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        wf_contents = textwrap.dedent("""\
        name: run_test
        kind: horus_workflow
        tasks:
            t:
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
            wf.run()

        mock_run.assert_called_once()

    def test_run_skips_task_when_all_outputs_exist(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        wf_contents = textwrap.dedent("""\
            name: skip_test
            kind: horus_workflow
            tasks:
                t:
                    name: Task
                    kind: horus_task
                    runtime:
                        kind: command
                        command: "echo test"
                    executor:
                        kind: shell
                    outputs:
                        output_file:
                            kind: file
                            path: /tmp/some_file.txt
            """)
        wf_file = make_workflow_file(tmp_path, wf_contents)
        wf = HorusWorkflow.from_yaml(wf_file)

        with (
            patch.object(FileArtifact, "exists", return_value=True),
            patch.object(HorusTask, "run") as mock_run,
        ):
            wf.run()

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_run_executes_tasks_in_order(
        self, mock_run: Mock, make_task: MakeTaskType
    ) -> None:

        mock_run.return_value = Mock(returncode=0)
        task_a = make_task(cmd="echo A")
        task_b = make_task(cmd="echo B")
        wf = HorusWorkflow(name="order_test", tasks={"a": task_a, "b": task_b})
        wf.run()

        # Two tasks, two calls
        function_calls = 2

        assert mock_run.call_count == function_calls

        calls = [call.args[0] for call in mock_run.call_args_list]
        assert calls == ["echo A", "echo B"]

    def test_run_stops_on_first_failure(self, make_task: MakeTaskType) -> None:

        class TaskWithFailure(HorusTask):
            add_to_registry = False

            def run(self) -> None:
                super().run()
                raise TaskExecutionError("fail")

        task_a = make_task(cmd="echxxo A")
        task_b = make_task(cmd="echo B", task_class=TaskWithFailure)

        wf = HorusWorkflow(name="stop_test", tasks={"a": task_a, "b": task_b})
        with pytest.raises(TaskExecutionError):
            wf.run()

        assert task_a.runs == 1
        assert task_b.runs == 0

    def test_run_empty_workflow(self) -> None:

        # Empty flow
        wf = HorusWorkflow(name="empty", tasks={})

        # Should complete without error
        wf.run()
