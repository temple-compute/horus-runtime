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
Implementation of the FolderArtifact class, which represents a local
folder/directory artifact in the Horus runtime.
"""

import json
from typing import Any, cast

from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.core.artifact.base import BaseArtifact


class JSONArtifact[T: Any = Any](BaseArtifact[T]):
    """
    Represents a JSON-serializable Python object artifact.
    The artifact is materialized as a JSON file on disk.
    """

    kind: str = "json"

    def read(self) -> T:
        """
        Read and deserialize the JSON artifact contents.

        Warning: This method assumes that the JSON file is well-formed and that
        the contents can be deserialized into the expected type `T`. For more
        robust handling, consider using PydanticArtifact, which provides
        validation and error handling for deserialization.
        """
        with open(self.path) as f:
            j_contet = json.load(f)
        self._emit_event(ArtifactEventsEnum.READ)

        return cast(T, j_contet)

    def write(self, value: T) -> None:
        """
        Serialize and write the JSON artifact contents.
        """
        with open(self.path, "w") as f:
            json.dump(value, f)
        self._emit_event(ArtifactEventsEnum.WRITE)
