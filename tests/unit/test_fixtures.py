"""
Test fixtures and configuration functionality
"""

import pytest


@pytest.mark.unit
def test_sample_fixture(sample_data):
    """
    Test using the sample fixture from conftest
    """

    assert sample_data["test"] == "data"
    assert sample_data["value"] == 42


@pytest.mark.unit
def test_pytest_configuration():
    """
    Test that pytest is properly configured
    """

    # This test verifies basic pytest functionality
    assert True
