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
The ``{value, label}`` option shared by value artifacts that let the user
pick among authored alternatives.
"""

import json

from pydantic import BaseModel

from horus_runtime.i18n import tr as _

type OptionValue = str | int | float | bool
"""A JSON scalar: what the task's runtime receives for the option."""


class Option(BaseModel):
    """
    One authored alternative of a value artifact.

    ``value`` is what reaches the runtime; ``label`` is only what the UI shows,
    falling back to ``str(value)`` when it is ``None``.
    """

    value: OptionValue
    label: str | None = None


def option_text(value: OptionValue) -> str:
    """
    The text an option value renders to, on disk and in ``$id``.

    Strings are written bare (like ``StringArtifact``) so ``--device $device``
    and ``cat $device`` both give ``gpu``; anything else is JSON (like
    ``NumberArtifact``/``BooleanArtifact``), which is what substitution
    renders for those types too.
    """
    return value if isinstance(value, str) else json.dumps(value)


def check_options(label: str, options: list[Option]) -> None:
    """
    Raise ValueError unless *options* is non-empty and distinct by text, so
    a file can always be read back to exactly one option.
    """
    if not options:
        raise ValueError(
            _("%(name)s needs at least one option.") % {"name": label}
        )
    seen: set[str] = set()
    for option in options:
        text = option_text(option.value)
        if text in seen:
            raise ValueError(
                _("%(name)s has duplicate option '%(v)s'.")
                % {"name": label, "v": text}
            )
        seen.add(text)


def match_option(
    label: str, options: list[Option], value: OptionValue
) -> OptionValue:
    """The option value matching *value*'s text, or ValueError."""
    text = option_text(value)
    for option in options:
        if option_text(option.value) == text:
            return option.value
    raise ValueError(
        _("%(name)s: '%(v)s' is not one of its options (%(options)s).")
        % {
            "name": label,
            "v": text,
            "options": ", ".join(option_text(o.value) for o in options),
        }
    )
