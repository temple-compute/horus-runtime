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
Unit tests for main module
"""

from unittest.mock import patch

import pytest

import src.main as main


@pytest.mark.unit
class TestMain:
    """
    Test cases for main module
    """

    @patch("builtins.print")
    def test_main_execution(self, mock_print):
        """
        Test that main execution prints expected message
        """

        # Call the main function directly
        main.main()

        # Verify the print was called with expected message
        mock_print.assert_called_with("Horus Runtime is starting...")

    def test_main_module_exists(self):
        """
        Test that main module can be imported
        """

        assert main is not None

    def test_main_function_exists(self):
        """
        Test that main function exists and is callable
        """

        assert hasattr(main, "main")
        assert callable(main.main)
