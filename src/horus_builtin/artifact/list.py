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
from typing import ClassVar, cast

from pydantic import Field

from horus_builtin.artifact.value import ValueArtifact
from horus_builtin.event.artifact_event import ArtifactEventsEnum

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
