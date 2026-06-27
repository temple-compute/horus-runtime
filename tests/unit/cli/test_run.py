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
Unit tests for the ``horus run`` CLI command.
"""

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from horus_runtime.cli import main


def _write_workflow(
    tmp_path: Path, *, command: str = "true", with_task: bool = True
) -> Path:
    """Write a minimal one-task workflow YAML and return its path."""
    if with_task:
        body = textwrap.dedent(f"""\
            name: cli_test_wf
            kind: horus_workflow
            tasks:
              - id: t1
                name: Task One
                kind: horus_task
                runtime:
                  kind: command
                  command: "{command}"
                executor:
                  kind: shell
            """)
    else:
        body = "name: empty_wf\nkind: horus_workflow\ntasks: []\n"
    wf = tmp_path / "workflow.yaml"
    wf.write_text(body)
    return wf


@pytest.mark.unit
class TestRunCommand:
    """Tests for ``horus run``."""

    def test_run_no_tui_succeeds(self, tmp_path: Path) -> None:
        """A valid workflow runs to completion and exits 0."""
        wf = _write_workflow(tmp_path)
        result = CliRunner().invoke(main, ["run", str(wf), "--no-tui"])
        assert result.exit_code == 0, result.output

    def test_run_with_tui_succeeds(self, tmp_path: Path) -> None:
        """The default (TUI) path also runs to completion."""
        wf = _write_workflow(tmp_path)
        result = CliRunner().invoke(main, ["run", str(wf)])
        assert result.exit_code == 0, result.output

    def test_run_explicit_trigger(self, tmp_path: Path) -> None:
        """An explicit --trigger is honored."""
        wf = _write_workflow(tmp_path)
        result = CliRunner().invoke(
            main, ["run", str(wf), "--trigger", "t1", "--no-tui"]
        )
        assert result.exit_code == 0, result.output

    def test_run_empty_workflow_errors(self, tmp_path: Path) -> None:
        """A workflow with no tasks exits non-zero with a clear message."""
        wf = _write_workflow(tmp_path, with_task=False)
        result = CliRunner().invoke(main, ["run", str(wf), "--no-tui"])
        assert result.exit_code != 0
        assert "no tasks" in result.output.lower()

    def test_run_failing_task_errors(self, tmp_path: Path) -> None:
        """A failing task surfaces as a non-zero CLI exit."""
        wf = _write_workflow(tmp_path, command="false")
        result = CliRunner().invoke(main, ["run", str(wf), "--no-tui"])
        assert result.exit_code != 0

    def test_run_missing_file_errors(self, tmp_path: Path) -> None:
        """A non-existent workflow path is rejected by click."""
        result = CliRunner().invoke(
            main, ["run", str(tmp_path / "nope.yaml"), "--no-tui"]
        )
        assert result.exit_code != 0
