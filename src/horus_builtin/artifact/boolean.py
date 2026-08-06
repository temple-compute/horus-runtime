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
Implementation of the BooleanArtifact class, which represents a boolean
value artifact in the Horus runtime.
"""

import json
from pathlib import Path
from typing import ClassVar, cast

from horus_builtin.artifact.value import ValueArtifact
from horus_builtin.event.artifact_event import ArtifactEventsEnum


class BooleanArtifact(ValueArtifact[bool]):
    """
    Represents a boolean (``true``/``false``) value artifact.

    The value is materialized as a JSON boolean on disk, so a task consuming
    ``$id`` reads a file whose contents are ``true`` or ``false``.
    """

    kind: str = "boolean"
    kind_name: ClassVar[str] = "Boolean"
    kind_description: ClassVar[str] = "A boolean value artifact."

    value: bool = False

    def read(self) -> bool:
        """
        Read and deserialize the boolean from the artifact file.
        """
        with open(self.path) as f:
            value = json.load(f)
        self._emit_event(ArtifactEventsEnum.READ)
        return cast(bool, value)

    def write(self, value: bool) -> None:
        """
        Serialize and write the boolean to the artifact file.
        """
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(value, f)
        self._emit_event(ArtifactEventsEnum.WRITE)
