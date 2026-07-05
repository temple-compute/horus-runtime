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
Unit tests for the ``anchor_local_paths`` hook on BaseRuntime and its
override in PythonScriptRuntime.
"""

from pathlib import Path
from typing import ClassVar

import pytest

from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python_script import PythonScriptRuntime
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.task.base import BaseTask


class _MinimalRuntime(BaseRuntime):
    """Concrete subclass with no local file fields."""

    add_to_registry: ClassVar[bool] = False
    kind: str = "minimal_test"

    async def _setup_runtime(self, _: "BaseTask") -> None:
        return None


@pytest.mark.unit
class TestBaseRuntimeAnchorLocalPaths:
    """The base no-op implementation does not raise for runtimes
    with no local files.
    """

    def test_base_noop_does_not_raise(self, tmp_path: Path) -> None:
        """BaseRuntime subclass with no file fields survives the hook call."""
        rt = _MinimalRuntime()
        rt.anchor_local_paths(tmp_path)

    def test_command_runtime_noop_does_not_raise(self, tmp_path: Path) -> None:
        """CommandRuntime has no local file fields; anchor is a safe no-op."""
        rt = CommandRuntime(command="echo hi")
        rt.anchor_local_paths(tmp_path)


@pytest.mark.unit
class TestPythonScriptRuntimeAnchorLocalPaths:
    """PythonScriptRuntime.anchor_local_paths resolves script against base."""

    def test_relative_script_is_resolved_against_base(
        self, tmp_path: Path
    ) -> None:
        """A relative script path is made absolute under the given base."""
        rt = PythonScriptRuntime(script=Path("scripts/job.py"))
        rt.anchor_local_paths(tmp_path)
        assert rt.script == (tmp_path / "scripts/job.py").resolve()
        assert rt.script.is_absolute()

    def test_absolute_script_is_left_untouched(self, tmp_path: Path) -> None:
        """An already-absolute script path is not modified."""
        abs_script = (tmp_path / "scripts/job.py").resolve()
        rt = PythonScriptRuntime(script=abs_script)
        rt.anchor_local_paths(tmp_path / "some_other_base")
        assert rt.script == abs_script

    def test_anchor_is_idempotent(self, tmp_path: Path) -> None:
        """Calling anchor_local_paths twice yields the same absolute path."""
        rt = PythonScriptRuntime(script=Path("scripts/job.py"))
        rt.anchor_local_paths(tmp_path)
        first = rt.script
        rt.anchor_local_paths(tmp_path)
        assert rt.script == first
