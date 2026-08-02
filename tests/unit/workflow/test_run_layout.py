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
from unittest.mock import MagicMock

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python_script import PythonScriptRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget
from tests.conftest import MakeWorkflowFileType


def _task(
    task_id: str,
    *,
    inputs: list[BaseArtifact] | None = None,
    outputs: list[BaseArtifact] | None = None,
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
        """run_directory = base_directory / orchestrator working_directory."""
        wf = HorusWorkflow(
            name="layout",
            tasks=[_task("t1")],
            orchestrator_target=LocalTarget(
                working_directory="workflow_results"
            ),
        )
        wf._base_directory = tmp_path
        assert wf.run_directory == (tmp_path / "workflow_results").resolve()

    def test_absolute_working_directory_wins(self, tmp_path: Path) -> None:
        """An absolute working_directory is used as-is, ignoring
        base_directory.
        """
        abs_wd = (tmp_path / "elsewhere").resolve()
        wf = HorusWorkflow(
            name="layout",
            tasks=[_task("t1")],
            orchestrator_target=LocalTarget(
                working_directory=abs_wd.as_posix()
            ),
        )
        wf._base_directory = tmp_path / "ignored"
        assert wf.run_directory == abs_wd

    def test_defaults_to_cwd_when_unanchored(self) -> None:
        """Programmatic workflow with no orchestrator defaults to CWD."""
        wf = HorusWorkflow(name="layout", tasks=[_task("t1")])
        assert wf.run_directory == Path.cwd().resolve()


@pytest.mark.unit
class TestResolveRunPaths:
    """Declared artifact paths anchor by whether some task produces them."""

    def test_output_anchors_under_run_root_input_under_base(
        self, tmp_path: Path
    ) -> None:
        """Produced outputs go under run_root; external inputs stay at base."""
        external = FileArtifact(
            id="receptor", path=Path("examples/receptor.pdb")
        )
        produced = FileArtifact(id="vina", path=Path("results/vina.tar.gz"))
        wf = HorusWorkflow(
            name="layout",
            tasks=[_task("prep", inputs=[external], outputs=[produced])],
            orchestrator_target=LocalTarget(
                working_directory="workflow_results"
            ),
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
        """Shared declared path lands at the same location for both tasks."""
        producer_out = FileArtifact(
            id="vina", path=Path("results/vina.tar.gz")
        )
        consumer_in = FileArtifact(id="vina", path=Path("results/vina.tar.gz"))
        wf = HorusWorkflow(
            name="layout",
            tasks=[
                _task("prep", outputs=[producer_out]),
                _task("dock", inputs=[consumer_in]),
            ],
            orchestrator_target=LocalTarget(
                working_directory="workflow_results"
            ),
        )
        wf._base_directory = tmp_path

        wf._resolve_run_paths()

        expected = (
            tmp_path / "workflow_results/results/vina.tar.gz"
        ).resolve()
        assert producer_out.path == expected
        assert consumer_in.path == expected

    def test_orchestrator_working_directory_becomes_absolute_run_root(
        self, tmp_path: Path
    ) -> None:
        """After _resolve_run_paths the orchestrator working_directory is absolute."""  # noqa: E501
        wf = HorusWorkflow(
            name="layout",
            tasks=[_task("t1")],
            orchestrator_target=LocalTarget(
                working_directory="workflow_results"
            ),
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
        """An artifact with an absolute declared path is not re-anchored."""
        abs_out = (tmp_path / "somewhere/out.txt").resolve()
        artifact = FileArtifact(id="out", path=abs_out)
        wf = HorusWorkflow(
            name="layout",
            tasks=[_task("t1", outputs=[artifact])],
            orchestrator_target=LocalTarget(
                working_directory="workflow_results"
            ),
        )
        wf._base_directory = tmp_path

        wf._resolve_run_paths()

        assert artifact.path == abs_out


@pytest.mark.unit
class TestRuntimeAnchorLocalPaths:
    """_resolve_run_paths delegates path anchoring to the runtime hook."""

    def _script_task(
        self, script: Path, base_dir: Path, working_dir: str = "results"
    ) -> tuple[HorusWorkflow, PythonScriptRuntime]:
        runtime = PythonScriptRuntime(script=script)
        task = HorusTask(
            id="t1",
            name="t1",
            runtime=runtime,
            executor=ShellExecutor(),
            target=LocalTarget(),
        )
        wf = HorusWorkflow(
            name="layout",
            tasks=[task],
            orchestrator_target=LocalTarget(working_directory=working_dir),
        )
        wf._base_directory = base_dir
        return wf, runtime

    def test_relative_script_is_anchored_to_base(self, tmp_path: Path) -> None:
        """Relative script resolves against the workflow base directory."""
        wf, runtime = self._script_task(Path("scripts/job.py"), tmp_path)
        wf._resolve_run_paths()
        assert runtime.script == (tmp_path / "scripts/job.py").resolve()

    def test_absolute_script_is_left_untouched(self, tmp_path: Path) -> None:
        """An already-absolute script path is not modified."""
        abs_script = (tmp_path / "scripts/job.py").resolve()
        wf, runtime = self._script_task(abs_script, tmp_path / "other_base")
        wf._resolve_run_paths()
        assert runtime.script == abs_script

    def test_command_runtime_survives_anchor_call(
        self, tmp_path: Path
    ) -> None:
        """CommandRuntime has no local files; anchor hook is a safe no-op."""
        task = HorusTask(
            id="t1",
            name="t1",
            runtime=CommandRuntime(command="echo hi"),
            executor=ShellExecutor(),
            target=LocalTarget(),
        )
        wf = HorusWorkflow(
            name="layout",
            tasks=[task],
            orchestrator_target=LocalTarget(working_directory="results"),
        )
        wf._base_directory = tmp_path
        wf._resolve_run_paths()  # must not raise

    def test_anchoring_is_target_agnostic(self, tmp_path: Path) -> None:
        """Swapping to an SSH-like target does not affect local path anchoring.

        anchor_local_paths resolves paths on the orchestrator side only,
        so the concrete target type is irrelevant.
        """
        ssh_target = MagicMock(spec=BaseTarget)
        ssh_target.working_directory = None
        ssh_target.location_id = "remote-host"

        runtime = PythonScriptRuntime(script=Path("scripts/job.py"))
        task = HorusTask(
            id="t1",
            name="t1",
            runtime=runtime,
            executor=ShellExecutor(),
            target=ssh_target,
        )
        wf = HorusWorkflow(
            name="layout",
            tasks=[task],
            orchestrator_target=LocalTarget(working_directory="results"),
        )
        wf._base_directory = tmp_path
        wf._resolve_run_paths()
        assert runtime.script == (tmp_path / "scripts/job.py").resolve()


@pytest.mark.unit
class TestFromYamlBaseDirectory:
    """from_yaml anchors the run at the workflow file's own directory."""

    def test_from_yaml_sets_base_directory_to_yaml_parent(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """from_yaml sets _base_directory to the YAML file's parent."""
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
        assert (
            wf.run_directory
            == (
                Path(workflow_file).resolve().parent / "workflow_results"
            ).resolve()
        )
