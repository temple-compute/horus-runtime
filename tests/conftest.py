"""
Test configuration for pytest
"""

import pytest


@pytest.fixture
def sample_data():
    """Sample test data fixture"""
    return {"test": "data", "value": 42}


def pytest_configure(config):
    """Configure pytest with custom markers"""
    config.addinivalue_line("markers", "unit: Unit tests")
    config.addinivalue_line("markers", "integration: Integration tests")
    config.addinivalue_line("markers", "slow: Slow running tests")
