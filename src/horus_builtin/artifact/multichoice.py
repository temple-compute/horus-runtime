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
Implementation of the MultiChoiceArtifact class, a value artifact whose value
is any subset of a list of authored options (chips or checkboxes in the GUI).
"""

import json
from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, model_validator

from horus_builtin.artifact.limits import check_count
from horus_builtin.artifact.options import (
    Option,
    OptionValue,
    check_options,
    match_option,
    option_text,
)
from horus_builtin.artifact.value import ValueArtifact
from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.i18n import tr as _


class MultiChoiceArtifact(ValueArtifact[list[OptionValue]]):
    """
    Represents any number of values picked from a fixed list of options.

    ``options`` is the domain, exactly as for ``ChoiceArtifact``. ``value``
    holds the picked option values in option order, without duplicates, so
    ``$id`` renders the same words however the picks were made. The file on
    disk is a JSON array.
    """

    kind: str = "multichoice"
    kind_name: ClassVar[str] = "Multiple choice"
    kind_description: ClassVar[str] = (
        "Any number of values picked from a list of options."
    )

    options: list[Option] = Field(default_factory=list)
    """The allowed values, each with an optional display label."""

    value: list[OptionValue] = Field(default_factory=list)
    """The picked option values; empty picks none."""

    min_picks: int | None = Field(default=None, ge=0)
    """The fewest options that must be picked (``None``: no minimum)."""

    max_picks: int | None = Field(default=None, ge=0)
    """The most options that may be picked (``None``: no maximum)."""

    @model_validator(mode="after")
    def _check_options(self) -> Self:
        check_options(self.id, self.options)
        self.value = self._picked(self.value)
        check_count(
            self.id,
            len(self.value),
            self.min_picks,
            self.max_picks,
            _("picks"),
        )
        return self

    def _picked(self, values: list[OptionValue]) -> list[OptionValue]:
        """*values* as option values in option order, or ValueError."""
        picked = {
            option_text(match_option(self.id, self.options, v)) for v in values
        }
        return [
            o.value for o in self.options if option_text(o.value) in picked
        ]

    def read(self) -> list[OptionValue]:
        """
        Read the file back to the option values it holds.
        """
        with open(self.path) as f:
            value = self._picked(json.load(f))
        self._emit_event(ArtifactEventsEnum.READ)
        return value

    def write(self, value: list[OptionValue]) -> None:
        """
        Write *value*, every item one of the options, as a JSON array.
        """
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._picked(value), f)
        self._emit_event(ArtifactEventsEnum.WRITE)
