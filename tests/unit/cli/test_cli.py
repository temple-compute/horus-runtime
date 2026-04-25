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
Unit tests for horus-runtime CLI module.
"""

from click.testing import CliRunner

from horus_runtime.cli import main
from horus_runtime.version import __version__ as horus_version


def test_cli_version_exposed_and_exits_successfully() -> None:
    """
    Ensure the Click CLI exposes the package version and exits 0.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])

    # Click's version output should include the package version
    assert result.exit_code == 0
    assert horus_version in result.output
