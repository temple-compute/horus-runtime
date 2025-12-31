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
