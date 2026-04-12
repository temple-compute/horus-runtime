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
Localization module for horus-runtime.

``make_translator`` is the single building block for all translation needs —
both inside this package and in external plugins::

    # horus-runtime internal (this file)
    tr = make_translator("horus_runtime", Path(__file__).parent / "locale")

    # Any plugin (three lines, no other boilerplate)
    from pathlib import Path
    from horus_runtime.i18n import make_translator
    tr = make_translator("my_plugin", Path(__file__).parent / "locale")

Import ``tr`` (aliased as ``_``) in any module that has user-visible strings::

    from horus_runtime.i18n import tr as _

    _("Task {name} started.", name=self.name)
    _("Processed {n} file", "Processed {n} files", n=count)
"""

import gettext
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _detect_locale() -> str:
    """
    Return the two-letter language code from the ``LANG`` env variable.
    """
    return os.environ.get("LANG", "en").split("_")[0]


def make_translator(domain: str, locale_dir: Path) -> Callable[..., str]:
    """
    Return a ``tr``: compatible callable bound to *domain* and *locale_dir*.

    ``gettext.translation`` is called once at definition time with
    ``fallback=True``, so an absent or unsupported locale silently returns
    the original (English) string — no ``FileNotFoundError`` handling needed
    at the call site.

    Args:
        domain:     gettext message domain (usually your package name).
        locale_dir: Directory containing ``<lang>/LC_MESSAGES/<domain>.mo``.

    Returns:
        Callable with signature
        ``(msg, plural=None, n=None, **kwargs) -> str``.
    """
    _t = gettext.translation(
        domain,
        localedir=str(locale_dir),
        languages=[_detect_locale()],
        fallback=True,
    )

    def _tr(
        msg: str,
        plural: str | None = None,
        n: int | None = None,
        **kwargs: Any,
    ) -> str:
        if n is not None and plural is not None:
            translated = _t.ngettext(msg, plural, n)
            return translated.format(n=n, **kwargs)

        message = _t.gettext(msg)
        return message.format(**kwargs) if kwargs else message

    return _tr


# Runtime's own translator. External plugins should create their own translator
# instances using this function.
tr = make_translator("horus_runtime", Path(__file__).parent / "locale")
