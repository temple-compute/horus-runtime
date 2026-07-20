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
Tests for PythonScriptRuntime and target-aware artifact substitution.
"""

import shlex
from pathlib import Path
from typing import cast

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.json import JSONArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python_script import PythonScriptRuntime
from horus_builtin.runtime.substitution import _ArtifactRef, substitute
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget


def _task(
    tmp_path: Path,
    runtime: CommandRuntime,
    output: JSONArtifact,
) -> HorusTask:
    return HorusTask(
        id="run",
        name="run",
        runtime=runtime,
        executor=ShellExecutor(),
        target=LocalTarget(working_directory=tmp_path.as_posix()),
        outputs=[output],
    )


@pytest.mark.usefixtures("horus_context")
async def test_python_script_ships_script_and_builds_command(
    tmp_path: Path,
) -> None:
    """The runtime puts the script on the target and runs it there."""
    script = tmp_path / "src" / "job.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('hi')\n")

    result = JSONArtifact(id="result", path=tmp_path / "out" / "result.json")
    task = _task(
        tmp_path,
        PythonScriptRuntime(script=script, args="$result"),
        result,
    )

    cmd = await task.runtime.setup_runtime(task)

    placed = Path(task.working_dir) / "job.py"
    # Script was shipped into the task's working dir on the target...
    assert placed.read_text() == "print('hi')\n"
    # ...and the command runs it with the output's on-target path appended —
    # no remote path constructed by the caller.
    assert cmd == f"python {placed} {result.path}"


@pytest.mark.usefixtures("horus_context")
async def test_python_script_quotes_remote_path_with_spaces(
    tmp_path: Path,
) -> None:
    """A working dir with spaces is shell-quoted, not split into two args."""
    work = tmp_path / "my project"
    work.mkdir()
    script = tmp_path / "job.py"
    script.write_text("print('hi')\n")

    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    task = HorusTask(
        id="run",
        name="run",
        runtime=PythonScriptRuntime(script=script),
        executor=ShellExecutor(),
        target=LocalTarget(working_directory=work.as_posix()),
        outputs=[result],
    )

    cmd = await task.runtime.setup_runtime(task)

    placed = Path(task.working_dir) / "job.py"
    assert cmd == f"python {shlex.quote(str(placed))}"
    assert "'" in cmd  # the space forced quoting, so the path is one arg


@pytest.mark.usefixtures("horus_context")
async def test_python_script_templated_script_uses_input_artifact(
    tmp_path: Path,
) -> None:
    """
    ``script: ${id}`` runs the input artifact already on the target, without
    reading the file from this machine.

    This is how an imported workflow runs on a host that never had the repo:
    the transfer layer materialises the script, so there is nothing to upload.
    """
    work = tmp_path / "work"
    work.mkdir()
    # Deliberately never created on the orchestrator side beyond the target
    # dir: the point is that _setup_runtime does not read it from here.
    staged = work / "job.py"
    staged.write_text("print('hi')\n")

    script_in = FileArtifact(id="job_script", path=staged)
    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    task = HorusTask(
        id="run",
        name="run",
        runtime=PythonScriptRuntime(
            script=Path("${job_script}"), args="$result"
        ),
        executor=ShellExecutor(),
        target=LocalTarget(working_directory=work.as_posix()),
        inputs=[script_in],
        outputs=[result],
    )

    cmd = await task.runtime.setup_runtime(task)

    assert cmd == f"python {staged} {result.path}"


def test_python_script_templated_script_is_not_anchored(
    tmp_path: Path,
) -> None:
    """A templated script names an artifact, so anchoring must leave it be."""
    runtime = PythonScriptRuntime(script=Path("${job_script}"))
    runtime.anchor_local_paths(tmp_path)
    assert str(runtime.script) == "${job_script}"

    # A plain relative path still anchors to the workflow directory.
    plain = PythonScriptRuntime(script=Path("scripts/job.py"))
    plain.anchor_local_paths(tmp_path)
    assert plain.script == (tmp_path / "scripts" / "job.py").resolve()


def test_artifact_ref_resolves_on_target_path(tmp_path: Path) -> None:
    """str(ref) -> path_on_target; ref.path/ref.id forward to the artifact."""

    class _StubTarget:
        def path_on_target(self, artifact: BaseArtifact) -> str:
            return f"/remote/{artifact.path.name}"

    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    ref = _ArtifactRef(
        # Ducktyping: _StubTarget has path_on_target() so it's a BaseTarget
        # for our purposes.
        result,
        cast(BaseTarget, _StubTarget()),
    )

    assert f"{ref}" == "/remote/result.json"
    assert str(ref) == "/remote/result.json"
    assert str(ref.path) == str(result.path)
    assert ref.id == "result"


def test_substitute_uses_local_path_for_local_target(
    tmp_path: Path,
) -> None:
    """On a LocalTarget, $result resolves to the local artifact path."""
    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    task = _task(tmp_path, CommandRuntime(command="x"), result)

    assert substitute("$result", task) == str(result.path)
    assert substitute("${result.path}", task) == str(result.path)
    assert substitute("${result.id}", task) == "result"
