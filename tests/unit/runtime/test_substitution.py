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
Unit tests for the shared ``substitute`` helper in
``horus_builtin.runtime.substitution``.
"""

from pathlib import Path

import pytest

from horus_builtin.artifact.json import JSONArtifact
from horus_builtin.executor.python_exec import PythonExecExecutor
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python_string import PythonCodeStringRuntime
from horus_builtin.runtime.substitution import substitute
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask


def _shell_task(
    tmp_path: Path, command: str, output: JSONArtifact
) -> HorusTask:
    return HorusTask(
        id="run",
        name="my_task",
        runtime=CommandRuntime(command=command),
        executor=ShellExecutor(),
        target=LocalTarget(working_directory=tmp_path.as_posix()),
        outputs=[output],
    )


def _python_task(tmp_path: Path, code: str, output: JSONArtifact) -> HorusTask:
    return HorusTask(
        id="run",
        name="my_task",
        runtime=PythonCodeStringRuntime(code=code),
        executor=PythonExecExecutor(),
        target=LocalTarget(working_directory=tmp_path.as_posix()),
        outputs=[output],
    )


def test_substitute_task_name(tmp_path: Path) -> None:
    """${task.name} resolves to the task's name field."""
    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    task = _shell_task(tmp_path, "unused", result)

    assert substitute("${task.name}", task) == "my_task"


def test_substitute_unknown_env_var_preserved(tmp_path: Path) -> None:
    """Shell $VAR that doesn't match an artifact id is left untouched."""
    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    task = _shell_task(tmp_path, "unused", result)

    assert substitute("echo $HOME", task) == "echo $HOME"


def test_substitute_double_dollar_escapes(tmp_path: Path) -> None:
    """$$ emits a literal $."""
    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    task = _shell_task(tmp_path, "unused", result)

    assert substitute("echo $$", task) == "echo $"


def test_substitute_curly_braces_preserved(tmp_path: Path) -> None:
    """Plain {} (e.g. shell brace expansion) passes through unchanged."""
    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    task = _shell_task(tmp_path, "unused", result)

    assert substitute("echo {a,b}", task) == "echo {a,b}"


def test_substitute_reserved_id_raises(tmp_path: Path) -> None:
    """An artifact with id='task' raises ValueError."""
    reserved = JSONArtifact(id="task", path=tmp_path / "task.json")
    task = HorusTask(
        id="run",
        name="my_task",
        runtime=CommandRuntime(command="x"),
        executor=ShellExecutor(),
        target=LocalTarget(working_directory=tmp_path.as_posix()),
        outputs=[reserved],
    )

    with pytest.raises(ValueError, match="reserved"):
        substitute("$task", task)


@pytest.mark.usefixtures("horus_context")
async def test_command_runtime_result_path_end_to_end(tmp_path: Path) -> None:
    """${result.path} in a CommandRuntime resolves end-to-end."""
    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    task = _shell_task(tmp_path, "cat ${result.path}", result)

    formatted = await task.runtime.setup_runtime(task)

    assert formatted == f"cat {result.path}"


@pytest.mark.usefixtures("horus_context")
async def test_python_code_string_result_path_end_to_end(
    tmp_path: Path,
) -> None:
    """${result.path} in PythonCodeStringRuntime resolves end-to-end."""
    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    task = _python_task(tmp_path, "open('${result.path}')", result)

    code = await task.runtime.setup_runtime(task)

    assert code == f"open('{result.path}')"
