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
Base definition for local file or folder artifacts in the Horus Runtime
"""

import hashlib
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field, model_validator
from typing_extensions import Self

from horus_runtime.core.interfaces.artifact import Artifact


class LocalPathArtifactBase(Artifact):
    """
    Common base class for local file or folder artifacts in the Horus runtime.
    """

    path: Annotated[Path, Field(default=Path(), validation_alias="uri")]
    """
    Path to the local File
    """

    @model_validator(mode="after")
    def _update_uri(self) -> Self:
        """
        After the model is initialized, update the uri field to match the path.
        This ensures that the uri field is always consistent with the path
        field.
        """

        # If the path is a URI, remove the URI scheme and convert it to a Path
        # object
        parsed = urlparse(self.uri)
        if parsed.scheme not in ("file", ""):
            raise ValueError(
                f"Unsupported URI scheme {parsed.scheme!r} for"
                f" {type(self).__name__}. Only 'file' scheme or plain paths"
                " without a scheme are supported."
            )

        self.path = Path(parsed.path)

        # Resolve the path to an absolute path and convert it back to
        # the correct URI.
        self.path = self.path.resolve()
        self.uri = self.path.as_uri()

        return self

    def exists(self) -> bool:
        """
        Checks if the file artifact exists at the specified path by delegating
        to the path's exists method.
        """
        return self.path.exists()

    def materialize(self) -> Path:
        return self.path

    @staticmethod
    def hash_file(path: Path) -> bytes:
        """
        Computes the hash of the file contents.
        """

        # Read in 64KB chunks to handle large files efficiently
        buffer = 65536

        # Use SHA-256 hash algorithm to compute the hash of the file content
        # SHA-256 provides non-colliding hashes and is widely used for
        # file integrity checks
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(buffer):
                sha256.update(chunk)

        return sha256.digest()
