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
# along with this program.  If not, see <https://www.gnu.org/licenses/>.#
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
