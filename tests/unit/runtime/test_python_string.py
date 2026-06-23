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
Tests for PythonCodeStringRuntime placeholder substitution.
"""

from pathlib import Path

import pytest

from horus_builtin.artifact.json import JSONArtifact
from horus_builtin.executor.python_exec import PythonExecExecutor
from horus_builtin.runtime.python_string import PythonCodeStringRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask


def _task(tmp_path: Path, code: str, output: JSONArtifact) -> HorusTask:
    return HorusTask(
        id="run",
        name="run",
        runtime=PythonCodeStringRuntime(code=code),
        executor=PythonExecExecutor(),
        target=LocalTarget(working_directory=tmp_path.as_posix()),
        outputs=[output],
    )


@pytest.mark.usefixtures("horus_context")
async def test_dollar_placeholder_resolves_to_on_target_path(
    tmp_path: Path,
) -> None:
    """$<id> substitutes to the artifact's on-target path."""
    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    task = _task(tmp_path, "open('$result')", result)

    assert await task.runtime.setup_runtime(task) == f"open('{result.path}')"


@pytest.mark.usefixtures("horus_context")
async def test_python_braces_are_left_untouched(tmp_path: Path) -> None:
    """Python ``{}`` (dict literals, f-strings) survive unchanged.

    Guards against the regression where ``str.format`` raised on snippets
    containing braces.
    """
    result = JSONArtifact(id="result", path=tmp_path / "result.json")
    code = 'd = {"a": 1}\nprint(f"{d}")'
    task = _task(tmp_path, code, result)

    assert await task.runtime.setup_runtime(task) == code
