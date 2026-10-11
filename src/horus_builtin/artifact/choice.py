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
Implementation of the ChoiceArtifact class, a value artifact whose value is
picked from a list of authored options (a dropdown in the GUI).
"""

import json
from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, model_validator

from horus_builtin.artifact.options import Option, OptionValue
from horus_builtin.artifact.value import ValueArtifact
from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.i18n import tr as _


def _text(value: OptionValue) -> str:
    """
    The text an option value renders to, on disk and in ``$id``.

    Strings are written bare (like ``StringArtifact``) so ``--device $device``
    and ``cat $device`` both give ``gpu``; anything else is JSON (like
    ``NumberArtifact``/``BooleanArtifact``), which is what substitution
    renders for those types too.
    """
    return value if isinstance(value, str) else json.dumps(value)


class ChoiceArtifact(ValueArtifact[OptionValue | None]):
    """
    Represents a value picked from a fixed list of options.

    ``options`` is the domain: non-empty, with values distinct by their
    rendered text (so a file can always be read back to exactly one option).
    ``value`` must be one of the option values and defaults to the first.
    """

    kind: str = "choice"
    kind_name: ClassVar[str] = "Choice"
    kind_description: ClassVar[str] = (
        "A value picked from a list of options (a dropdown)."
    )

    options: list[Option] = Field(default_factory=list)
    """The allowed values, each with an optional display label."""

    value: OptionValue | None = None
    """The selected option's value; ``None`` selects the first option."""

    @model_validator(mode="after")
    def _check_options(self) -> Self:
        if not self.options:
            raise ValueError(
                _("Choice artifact '%(id)s' needs at least one option.")
                % {"id": self.id}
            )
        seen: set[str] = set()
        for option in self.options:
            text = _text(option.value)
            if text in seen:
                raise ValueError(
                    _("Choice artifact '%(id)s' has duplicate option '%(v)s'.")
                    % {"id": self.id, "v": text}
                )
            seen.add(text)
        self.value = (
            self.options[0].value
            if self.value is None
            else self._option_value(self.value)
        )
        return self

    def _option_value(self, value: OptionValue) -> OptionValue:
        """The option value matching *value*'s text, or ValueError."""
        text = _text(value)
        for option in self.options:
            if _text(option.value) == text:
                return option.value
        raise ValueError(
            _(
                "Choice artifact '%(id)s': '%(v)s' is not one of its options "
                "(%(options)s)."
            )
            % {
                "id": self.id,
                "v": text,
                "options": ", ".join(_text(o.value) for o in self.options),
            }
        )

    def read(self) -> OptionValue:
        """
        Read the file back to the option value it holds.
        """
        value = self._option_value(self.path.read_text())
        self._emit_event(ArtifactEventsEnum.READ)
        return value

    def write(self, value: OptionValue | None) -> None:
        """
        Write *value*, which must be one of the options, to the file.
        """
        if value is None:
            raise ValueError(
                _("Choice artifact '%(id)s' cannot write None.")
                % {"id": self.id}
            )
        text = _text(self._option_value(value))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text)
        self._emit_event(ArtifactEventsEnum.WRITE)
