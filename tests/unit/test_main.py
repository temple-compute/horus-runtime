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
Unit tests for main module.
"""

import pytest

from horus_runtime.cli import main as horus_runtime


@pytest.mark.unit
class TestMain:
    """
    Test cases for main module.
    """

    def test_main_module_exists(self) -> None:
        """
        Test that main module can be imported.
        """
        assert horus_runtime is not None

    def test_main_function_callable(self) -> None:
        """
        Test that main function is callable.
        """
        assert callable(horus_runtime)
