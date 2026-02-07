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
Implementation of the FileArtifact class, which represents a local
file artifact in the Horus runtime.
"""

from typing import Literal

from horus_runtime.core.artifact.local_artifact_base import (
    LocalPathArtifactBase,
)


class FileArtifact(LocalPathArtifactBase):
    """
    Represents a local file artifact.
    """

    add_to_registry = True

    kind: Literal["file"] = "file"

    @property
    def hash(self) -> str | None:
        # For file artifacts, the hash is computed based on the file contents.
        # Convert using hex to ensure it's a string representation of the hash.
        return self.hash_file(self.path).hex() if self.exists() else None
