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
Tests for the TargetCommandMiddleware domain: middleware may rewrite the
command string before it is dispatched by ``BaseTarget.run_command``.
"""

from collections.abc import Generator
from pathlib import Path

import pytest

from horus_builtin.target.local import LocalTarget
from horus_runtime.middleware.target_command import (
    TargetCommandMiddleware,
    TargetCommandMiddlewareContext,
)


@pytest.fixture
def restore_target_command_registry() -> Generator[None]:
    """
    Restore the target-command middleware registry after each test.
    """
    original_registry = list(TargetCommandMiddleware.registry)
    try:
        yield
    finally:
        TargetCommandMiddleware.registry = original_registry


@pytest.mark.unit
class TestTargetCommandMiddleware:
    """The command a target runs is the one middleware rewrote."""

    async def test_middleware_rewrites_command(
        self, tmp_path: Path, restore_target_command_registry: None
    ) -> None:
        """
        A middleware that overwrites ``ctx.command`` changes what actually
        runs on the target.
        """
        del restore_target_command_registry

        class RewriteMiddleware(TargetCommandMiddleware):
            """Replace whatever command is passed with a known marker."""

            async def before(
                self, context: TargetCommandMiddlewareContext
            ) -> None:
                context.command = "echo REWRITTEN"

        # Defining the concrete subclass above auto-registers it into
        # TargetCommandMiddleware.registry; the fixture restores it afterwards.

        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("echo ORIGINAL")
        stdout, _stderr = await proc.communicate()

        assert b"REWRITTEN" in stdout
        assert b"ORIGINAL" not in stdout

    async def test_no_middleware_runs_command_unchanged(
        self, tmp_path: Path
    ) -> None:
        """
        With no command middleware registered the original command runs.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("echo ORIGINAL")
        stdout, _stderr = await proc.communicate()

        assert b"ORIGINAL" in stdout
