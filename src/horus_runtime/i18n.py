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
Centralized localization module for horus-runtime.

This module implements a strict type-safe localization system where:
- Only this module directly calls gettext functions
- All locale utility functions definitions are centralized here
"""

import gettext
import os
from enum import Enum
from pathlib import Path
from typing import Any


class _HorusLocales(str, Enum):
    """
    Supported locales for horus-runtime.
    """

    ENGLISH = "en"
    SPANISH = "es"


class _LocaleUtils:
    """
    Utility functions for locale detection and management.
    """

    @staticmethod
    def get_system_locale() -> str:
        """
        Detect the system locale from the LANG environment variable.

        Returns:
            Locale code as string (e.g., 'en', 'es')
        """

        # Get system locale from environment variables
        # Usually, the LANG variable is in the format 'en_US.UTF-8'
        return os.environ.get("LANG", "en").split("_")[0]

    @staticmethod
    def get_default_locale() -> _HorusLocales:
        """
        Determine the default locale for the application.
        If the system locale is unsupported, defaults to English.

        Returns:
            _HorusLocales enum member
        """
        system_locale = _LocaleUtils.get_system_locale()

        try:
            return _HorusLocales(system_locale)
        except ValueError:
            return _HorusLocales.ENGLISH


class _HorusTranslationManager:
    """
    Sets up and manages the translation system for horus-runtime.
    """

    # Translation object used for all translations
    _current_translation: (
        gettext.NullTranslations | gettext.GNUTranslations
    ) = gettext.NullTranslations()

    def __init__(
        self, lang: _HorusLocales = _LocaleUtils.get_default_locale()
    ) -> None:
        """
        Initialize the translation manager with the default locale.
        """
        self._setup_locale(lang)

    def _setup_locale(
        self,
        lang: _HorusLocales,
    ) -> None:
        """
        Initialize the localization system.

        Args:
            lang: Language code (e.g., 'es', 'fr', 'de').
            If None, uses system locale.

        This should be called once during application startup, before any
        translation functions are used.
        """

        # Get the package directory to locate translation files
        package_dir = Path(__file__).parent
        locale_dir = package_dir / "locale"

        try:
            # Install the translation domain
            translation = gettext.translation(
                "horus_runtime",
                localedir=str(locale_dir),
                languages=[lang],
                fallback=True,
            )
            # Store the translation object for module-level access
            self._current_translation = translation

        except FileNotFoundError:
            # Fall back to NullTranslations (returns original strings)
            self._current_translation = gettext.NullTranslations()

    def translate(
        self,
        msg: str,
        plural: str | None = None,
        n: int | None = None,
        **kwargs: Any,
    ) -> str:
        """
        Translate and format a dynamic message.

        For automatic extraction of translatable strings, this function
        must be imported as "_" in modules that use it.


        Args:
            msg: Message string
            plural: Plural form of the message string
            n: Number for pluralization
            **kwargs: Format variables for string substitution

        Returns:
            Translated and formatted string

        Example::
            ```python
            from horus_runtime.i18n import tr as _

            _("Loaded {filename}", filename="data.csv")
            _("Processed {n} file", "Processed {n} files", n=3)
            _("User {user} logged in", user=username)
            ```
        """

        if n is not None and plural is not None:
            translated = self._current_translation.ngettext(msg, plural, n)
            return translated.format(n=n, **kwargs)

        message = self._current_translation.gettext(msg)
        if kwargs:
            message = message.format(**kwargs)

        return message


# Setup a single global translation manager instance
_translation_manager = _HorusTranslationManager()


def tr(
    msg: str,
    plural: str | None = None,
    n: int | None = None,
    **kwargs: Any,
) -> str:
    """
    Module-level translation function.

    This function wraps the translation manager's translate method
    for easy access throughout the application.

    Args:
        msg: Message string
        plural: Plural form of the message string
        n: Number for pluralization
        **kwargs: Format variables for string substitution
    Returns:
        Translated and formatted string
    """
    translated_message = _translation_manager.translate(
        msg,
        plural=plural,
        n=n,
        **kwargs,
    )

    return translated_message
