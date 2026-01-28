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
Unit tests for i18n module
"""

# We disable protected-access warnings for testing private members
# pyright: reportPrivateUsage=false
# pylint: disable=protected-access

import gettext
import os
from unittest.mock import MagicMock, patch

import pytest

from horus_runtime.i18n import (
    _HorusLocales,
    _HorusTranslationManager,
    _LocaleUtils,
    tr,
)


@pytest.mark.unit
class TestHorusLocales:
    """
    Test cases for _HorusLocales enum
    """

    def test_english_locale(self) -> None:
        """
        Test English locale constant
        """
        assert _HorusLocales.ENGLISH == "en"

    def test_spanish_locale(self) -> None:
        """
        Test Spanish locale constant
        """
        assert _HorusLocales.SPANISH == "es"

    def test_locale_is_string(self) -> None:
        """
        Test that locale values are strings
        """
        assert isinstance(_HorusLocales.ENGLISH.value, str)
        assert isinstance(_HorusLocales.SPANISH.value, str)


@pytest.mark.unit
class TestLocaleUtils:
    """
    Test cases for _LocaleUtils class
    """

    @patch.dict(os.environ, {"LANG": "es_ES.UTF-8"})
    def test_get_system_locale_spanish(self) -> None:
        """
        Test system locale detection for Spanish
        """
        locale = _LocaleUtils.get_system_locale()
        assert locale == "es"

    @patch.dict(os.environ, {"LANG": "en_US.UTF-8"})
    def test_get_system_locale_english(self) -> None:
        """
        Test system locale detection for English
        """
        locale = _LocaleUtils.get_system_locale()
        assert locale == "en"

    @patch.dict(os.environ, {}, clear=True)
    def test_get_system_locale_default(self) -> None:
        """
        Test system locale defaults to English when LANG is not set
        """
        locale = _LocaleUtils.get_system_locale()
        assert locale == "en"

    @patch("horus_runtime.i18n._LocaleUtils.get_system_locale")
    def test_get_default_locale_supported(
        self, mock_get_system: MagicMock
    ) -> None:
        """
        Test default locale when system locale is supported
        """
        mock_get_system.return_value = "es"
        locale = _LocaleUtils.get_default_locale()
        assert locale == _HorusLocales.SPANISH

    @patch("horus_runtime.i18n._LocaleUtils.get_system_locale")
    def test_get_default_locale_unsupported(
        self, mock_get_system: MagicMock
    ) -> None:
        """
        Test default locale falls back to English for unsupported locales
        """
        mock_get_system.return_value = "made_up_locale"
        locale = _LocaleUtils.get_default_locale()
        assert locale == _HorusLocales.ENGLISH


@pytest.mark.unit
class TestHorusTranslationManager:
    """
    Test cases for _HorusTranslationManager class
    """

    def test_manager_initialization(self) -> None:
        """
        Test translation manager can be initialized
        """
        manager = _HorusTranslationManager()
        assert manager is not None
        assert manager._current_translation is not None

    @patch("gettext.translation")
    def test_setup_locale_success(self, mock_translation: MagicMock) -> None:
        """
        Test successful locale setup
        """
        mock_trans = MagicMock(spec=gettext.GNUTranslations)
        mock_translation.return_value = mock_trans

        # Calls the constructor which in turn calls _setup_locale
        manager = _HorusTranslationManager(lang=_HorusLocales.SPANISH)

        mock_translation.assert_called_once()
        assert manager._current_translation == mock_trans

        # Verify that translation was set up for the correct locale
        _, kwargs = mock_translation.call_args
        assert kwargs["languages"] == [_HorusLocales.SPANISH.value]

    @patch("gettext.translation")
    def test_setup_locale_file_not_found(
        self, mock_translation: MagicMock
    ) -> None:
        """
        Test locale setup falls back when translation files not found
        """
        mock_translation.side_effect = FileNotFoundError()

        manager = _HorusTranslationManager()

        assert isinstance(
            manager._current_translation, gettext.NullTranslations
        )

    def test_translate_simple(self) -> None:
        """
        Test simple message translation
        """
        manager = _HorusTranslationManager()
        result = manager.translate("Hello")
        assert result == "Hello"  # NullTranslations returns original

    def test_translate_with_formatting(self) -> None:
        """
        Test message translation with formatting
        """
        manager = _HorusTranslationManager()
        result = manager.translate("Hello {name}", name="World")
        assert result == "Hello World"

    def test_translate_plural(self) -> None:
        """
        Test plural message translation
        """
        manager = _HorusTranslationManager()
        result = manager.translate("Found {n} file", "Found {n} files", n=1)
        assert result == "Found 1 file"

        result = manager.translate("Found {n} file", "Found {n} files", n=3)
        assert result == "Found 3 files"

    def test_translate_plural_with_formatting(self) -> None:
        """
        Test plural translation with additional formatting
        """
        manager = _HorusTranslationManager()
        result = manager.translate(
            "Found {n} file in {dir}",
            "Found {n} files in {dir}",
            n=2,
            dir="/tmp",
        )
        assert result == "Found 2 files in /tmp"

        result = manager.translate(
            "Found {n} file in {dir}",
            "Found {n} files in {dir}",
            n=1,
            dir="/var",
        )
        assert result == "Found 1 file in /var"


@pytest.mark.unit
class TestTranslationFunction:
    """
    Test cases for tr function (public API)
    """

    def test_tr_simple_message(self) -> None:
        """
        Test tr function with simple message
        """
        result = tr("Hello World")
        assert result == "Hello World"

    def test_tr_with_formatting(self) -> None:
        """
        Test tr function with string formatting
        """
        result = tr("Hello {name}", name="Alice")
        assert result == "Hello Alice"

    def test_tr_plural_singular(self) -> None:
        """
        Test tr function with plural (singular case)
        """
        result = tr("Found {n} item", "Found {n} items", n=1)
        assert result == "Found 1 item"

    def test_tr_plural_multiple(self) -> None:
        """
        Test tr function with plural (multiple case)
        """
        result = tr("Found {n} item", "Found {n} items", n=5)
        assert result == "Found 5 items"

    def test_tr_complex_formatting(self) -> None:
        """
        Test tr function with complex formatting
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
        Test tr function without any formatting arguments
        """
        result = tr("Simple message")
        assert result == "Simple message"

    def test_tr_empty_kwargs(self) -> None:
        """
        Test tr function with empty keyword arguments
        """
        result = tr("Message without format")
        assert result == "Message without format"
