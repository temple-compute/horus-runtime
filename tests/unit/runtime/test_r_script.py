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
Tests for RScriptRuntime.
"""

from pathlib import Path

import pytest
from pydantic import BaseModel

from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.r_script import RScriptRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.runtime.base import BaseRuntime


@pytest.mark.usefixtures("horus_context")
async def test_r_script_runs_the_script_with_rscript(tmp_path: Path) -> None:
    """The script is shipped to the target and run with ``Rscript``."""
    script = tmp_path / "src" / "job.R"
    script.parent.mkdir(parents=True)
    script.write_text("cat('hi')\n")

    task = HorusTask(
        id="run",
        name="run",
        runtime=RScriptRuntime(script=script, args="--verbose"),
        executor=ShellExecutor(),
        target=LocalTarget(working_directory=tmp_path.as_posix()),
    )

    cmd = await task.runtime.setup_runtime(task)

    placed = Path(task.working_dir) / "job.R"
    assert placed.read_text() == "cat('hi')\n"
    assert cmd == f"Rscript {placed} --verbose"


@pytest.mark.unit
def test_r_script_validates_by_kind() -> None:
    """``kind: r_script`` resolves to RScriptRuntime through the registry."""

    class _Model(BaseModel):
        runtime: BaseRuntime

    result = _Model.model_validate(
        {"runtime": {"kind": "r_script", "script": "analysis.R"}}
    )

    assert BaseRuntime.registry["r_script"] is RScriptRuntime
    assert isinstance(result.runtime, RScriptRuntime)
    assert result.runtime.python == "Rscript"


@pytest.mark.unit
def test_r_script_interpreter_is_overridable_and_titled() -> None:
    """The ``python`` key sets the interpreter, titled "Interpreter"."""
    runtime = RScriptRuntime(script=Path("a.R"), python="/opt/R/bin/Rscript")

    assert runtime.python == "/opt/R/bin/Rscript"
    schema = RScriptRuntime.model_json_schema()
    assert schema["properties"]["python"]["title"] == "Interpreter"
