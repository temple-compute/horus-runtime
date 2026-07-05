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
Unit tests for run-directory anchoring: per-task working dirs, declared output
artifacts, and external inputs are resolved into a single self-contained run
folder anchored at the workflow's base directory.
"""

import textwrap
from pathlib import Path

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python_script import PythonScriptRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from tests.conftest import MakeWorkflowFileType


def _task(
    task_id: str,
    *,
    inputs: list[FileArtifact] | None = None,
    outputs: list[FileArtifact] | None = None,
) -> HorusTask:
    return HorusTask(
        id=task_id,
        name=task_id,
        inputs=inputs or [],
        outputs=outputs or [],
        runtime=CommandRuntime(command="echo hi"),
        executor=ShellExecutor(),
        target=LocalTarget(),
    )


@pytest.mark.unit
class TestRunDirectory:
    """The run root is base_directory / orchestrator working_directory."""

    def test_run_directory_joins_base_and_working_directory(
        self, tmp_path: Path
    ) -> None:
        wf = HorusWorkflow(
            name="layout",
            tasks=[_task("t1")],
            orchestrator_target=LocalTarget(working_directory="workflow_results"),
        )
        wf._base_directory = tmp_path
        assert wf.run_directory == (tmp_path / "workflow_results").resolve()

    def test_absolute_working_directory_wins(self, tmp_path: Path) -> None:
        abs_wd = (tmp_path / "elsewhere").resolve()
        wf = HorusWorkflow(
            name="layout",
            tasks=[_task("t1")],
            orchestrator_target=LocalTarget(working_directory=abs_wd.as_posix()),
        )
        wf._base_directory = tmp_path / "ignored"
        assert wf.run_directory == abs_wd

    def test_defaults_to_cwd_when_unanchored(self) -> None:
        # No base directory (programmatic workflow) and no orchestrator
        # working directory: fall back to the process CWD.
        wf = HorusWorkflow(name="layout", tasks=[_task("t1")])
        assert wf.run_directory == Path.cwd().resolve()


@pytest.mark.unit
class TestResolveRunPaths:
    """Declared artifact paths anchor by whether some task produces them."""

    def test_output_anchors_under_run_root_input_under_base(
        self, tmp_path: Path
    ) -> None:
        external = FileArtifact(id="receptor", path="examples/receptor.pdb")
        produced = FileArtifact(id="vina", path="results/vina.tar.gz")
        wf = HorusWorkflow(
            name="layout",
            tasks=[_task("prep", inputs=[external], outputs=[produced])],
            orchestrator_target=LocalTarget(working_directory="workflow_results"),
        )
        wf._base_directory = tmp_path

        wf._resolve_run_paths()

        run_root = (tmp_path / "workflow_results").resolve()
        # Produced output nests under the run root...
        assert produced.path == (run_root / "results/vina.tar.gz").resolve()
        # ...while an external (never-produced) input stays at the base dir.
        assert external.path == (tmp_path / "examples/receptor.pdb").resolve()

    def test_intermediate_resolves_to_same_path_for_both_tasks(
        self, tmp_path: Path
    ) -> None:
        # The same declared path is one task's output and the next task's
        # input; both must land at one location (under the run root).
        producer_out = FileArtifact(id="vina", path="results/vina.tar.gz")
        consumer_in = FileArtifact(id="vina", path="results/vina.tar.gz")
        wf = HorusWorkflow(
            name="layout",
            tasks=[
                _task("prep", outputs=[producer_out]),
                _task("dock", inputs=[consumer_in]),
            ],
            orchestrator_target=LocalTarget(working_directory="workflow_results"),
        )
        wf._base_directory = tmp_path

        wf._resolve_run_paths()

        expected = (tmp_path / "workflow_results/results/vina.tar.gz").resolve()
        assert producer_out.path == expected
        assert consumer_in.path == expected

    def test_orchestrator_working_directory_becomes_absolute_run_root(
        self, tmp_path: Path
    ) -> None:
        wf = HorusWorkflow(
            name="layout",
            tasks=[_task("t1")],
            orchestrator_target=LocalTarget(working_directory="workflow_results"),
        )
        wf._base_directory = tmp_path

        wf._resolve_run_paths()

        assert wf.orchestrator_target is not None
        assert (
            wf.orchestrator_target.working_directory
            == (tmp_path / "workflow_results").resolve().as_posix()
        )

    def test_absolute_declared_path_is_left_untouched(
        self, tmp_path: Path
    ) -> None:
        abs_out = (tmp_path / "somewhere/out.txt").resolve()
        artifact = FileArtifact(id="out", path=abs_out.as_posix())
        wf = HorusWorkflow(
            name="layout",
            tasks=[_task("t1", outputs=[artifact])],
            orchestrator_target=LocalTarget(working_directory="workflow_results"),
        )
        wf._base_directory = tmp_path

        wf._resolve_run_paths()

        assert artifact.path == abs_out


@pytest.mark.unit
class TestFromYamlBaseDirectory:
    """from_yaml anchors the run at the workflow file's own directory."""

    def test_from_yaml_sets_base_directory_to_yaml_parent(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        wf_content = textwrap.dedent("""\
            name: yaml_layout
            kind: horus_workflow
            tasks:
                - id: t1
                  name: Task 1
                  kind: horus_task
                  runtime:
                      kind: command
                      command: "echo hello"
                  executor:
                      kind: shell
            orchestrator_target:
                kind: local
                working_directory: workflow_results
        """)
        workflow_file = make_workflow_file(tmp_path, wf_content)

        wf = HorusWorkflow.from_yaml(workflow_file)

        assert wf._base_directory == Path(workflow_file).resolve().parent
        # And that base dir drives the run root, independent of the CWD.
        assert wf.run_directory == (
            Path(workflow_file).resolve().parent / "workflow_results"
        ).resolve()
