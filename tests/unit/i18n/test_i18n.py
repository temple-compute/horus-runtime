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
Unit tests for i18n module.
"""

import pytest

from horus_runtime.i18n import tr


@pytest.mark.unit
class TestTranslationFunction:
    """
    Test cases for tr function (public API).
    """

    def test_tr_simple_message(self) -> None:
        """
        Test tr function with simple message.
        """
        result = tr("Hello World")
        assert result == "Hello World"

    def test_tr_with_formatting(self) -> None:
        """
        Test tr function with string formatting.
        """
        result = tr("Hello {name}", name="Alice")
        assert result == "Hello Alice"

    def test_tr_plural_singular(self) -> None:
        """
        Test tr function with plural (singular case).
        """
        result = tr("Found {n} item", "Found {n} items", n=1)
        assert result == "Found 1 item"

    def test_tr_plural_multiple(self) -> None:
        """
        Test tr function with plural (multiple case).
        """
        result = tr("Found {n} item", "Found {n} items", n=5)
        assert result == "Found 5 items"

    def test_tr_complex_formatting(self) -> None:
        """
        Test tr function with complex formatting.
        """
        result = tr(
            "User {user} processed {n} file in {location}",
            "User {user} processed {n} files in {location}",
            n=3,
            user="admin",
            location="/data",
        )
        assert result == "User admin processed 3 files in /data"

    def test_tr_no_formatting(self) -> None:
        """
        Test tr function without any formatting arguments.
        """
        result = tr("Simple message")
        assert result == "Simple message"

    def test_tr_empty_kwargs(self) -> None:
        """
        Test tr function with empty keyword arguments.
        """
        result = tr("Message without format")
        assert result == "Message without format"
