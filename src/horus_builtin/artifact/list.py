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
Implementation of the ListArtifact class, a value artifact holding a free
list of scalars (a chips input in the GUI).
"""

import json
from pathlib import Path
from typing import ClassVar, Self, cast

from pydantic import Field, model_validator

from horus_builtin.artifact.limits import (
    check_count,
    check_excluded,
    check_range,
)
from horus_builtin.artifact.value import ValueArtifact
from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.i18n import tr as _

ListItem = str | int | float


class ListArtifact(ValueArtifact[list[ListItem]]):
    """
    Represents a list of strings or numbers (seeds, temperatures, residue
    IDs).

    The value is materialized as a JSON array on disk; ``$id`` renders the
    items as separate shell-quoted words.
    """

    kind: str = "list"
    kind_name: ClassVar[str] = "List"
    kind_description: ClassVar[str] = "A list of strings or numbers."

    value: list[ListItem] = Field(default_factory=list)

    min_items: int | None = Field(default=None, ge=0)
    """The fewest items allowed (``None``: no minimum)."""

    max_items: int | None = Field(default=None, ge=0)
    """The most items allowed (``None``: no maximum)."""

    unique: bool = False
    """Reject a list that holds the same item twice."""

    min: int | float | None = None
    """The smallest allowed item; items must then be numbers."""

    max: int | float | None = None
    """The largest allowed item; items must then be numbers."""

    exclude: list[ListItem] = Field(default_factory=list)
    """Items that are never allowed."""

    @model_validator(mode="after")
    def _check_items(self) -> Self:
        check_count(
            self.name or self.id,
            len(self.value),
            self.min_items,
            self.max_items,
            _("items"),
        )
        bounded = self.min is not None or self.max is not None
        seen: set[ListItem] = set()
        for item in self.value:
            if bounded:
                if isinstance(item, str):
                    raise ValueError(
                        _("%(name)s: '%(v)s' is not a number.")
                        % {"name": self.name or self.id, "v": item}
                    )
                check_range(self.name or self.id, item, self.min, self.max)
            check_excluded(self.name or self.id, item, self.exclude)
            if self.unique and item in seen:
                raise ValueError(
                    _("%(name)s holds '%(v)s' twice.")
                    % {"name": self.name or self.id, "v": item}
                )
            seen.add(item)
        return self

    def read(self) -> list[ListItem]:
        """
        Read and deserialize the list from the artifact file.
        """
        with open(self.path) as f:
            value = json.load(f)
        self._emit_event(ArtifactEventsEnum.READ)
        return cast(list[ListItem], value)

    def write(self, value: list[ListItem]) -> None:
        """
        Serialize and write the list to the artifact file.
        """
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(value, f)
        self._emit_event(ArtifactEventsEnum.WRITE)
