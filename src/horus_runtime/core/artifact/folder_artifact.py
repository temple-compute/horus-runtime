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

import hashlib
from typing import Literal

from horus_runtime.core.artifact.local_artifact_base import (
    LocalPathArtifactBase,
)


class FolderArtifact(LocalPathArtifactBase):
    """
    Represents a local folder artifact.
    """

    add_to_registry = True

    kind: Literal["folder"] = "folder"

    def exists(self) -> bool:
        """
        Check if the folder specified by the path exists and is a directory.
        """
        return super().exists() and self.path.is_dir()

    @property
    def hash(self) -> str | None:
        """
        Computes the hash of the folder and its contents by recursively hashing
        all files in the folder. The hash is computed by combining the relative
        paths and contents of all files in the folder, ensuring that changes to
        any file or the addition/removal of files will result in a different
        hash.
        """

        if not self.exists():
            return None

        sha256 = hashlib.sha256()

        # 1. Get all files and sort them by relative path for determinism
        # We use rglob("*") to get everything, but only hash files
        paths = sorted([p for p in self.path.rglob("*") if p.is_file()])

        for path in paths:
            relative_path = path.relative_to(self.path).as_posix()
            sha256.update(relative_path.encode("utf-8"))

            # Hash the file contents
            sha256.update(self.hash_file(path))

        return sha256.hexdigest()
