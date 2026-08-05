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
Implementation of the StringArtifact class, which represents a string
value artifact in the Horus runtime.
"""

from pathlib import Path
from typing import ClassVar

from horus_builtin.artifact.value import ValueArtifact
from horus_builtin.event.artifact_event import ArtifactEventsEnum


class StringArtifact(ValueArtifact[str]):
    """
    Represents a string value artifact.

    The value is materialized as plain text on disk, so a task consuming
    ``$id`` reads a file whose contents are the string as authored (no
    quotes).
    """

    kind: str = "string"
    kind_name: ClassVar[str] = "String"
    kind_description: ClassVar[str] = "A string value artifact."

    value: str = ""

    def read(self) -> str:
        """
        Read and deserialize the string from the artifact file.
        """
        text = self.path.read_text()
        self._emit_event(ArtifactEventsEnum.READ)
        return text

    def write(self, value: str) -> None:
        """
        Serialize and write the string to the artifact file.
        """
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(value)
        self._emit_event(ArtifactEventsEnum.WRITE)
