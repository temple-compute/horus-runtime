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
from typing import Any
from urllib.parse import urlparse

from pydantic import model_validator
from typing_extensions import Self

from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.i18n import tr as _


class LocalPathArtifactBase(BaseArtifact):
    """
    Common base class for local file or folder artifacts in the Horus runtime.
    """

    path: Path = Path()
    """
    Path to the local file or folder.
    """

    uri: str = ""
    """
    URI can be optionally provided instead of path. If path is provided,
    the URI will be derived from it automatically.
    """

    add_to_registry = False

    @model_validator(mode="before")
    @classmethod
    def _derive_path_from_uri(cls, data: dict[str, Any]) -> dict[str, Any]:
        """
        Before the model is initialized, if no path is provided, derive it
        from the URI. This ensures that path is always set after construction.
        """
        # If path is not provided, attempt to derive it from the URI
        if "path" not in data or data["path"] is None:

            # "" evaluates to False if uri is None or not provided
            uri = str(data.get("uri", ""))
            if not uri:
                raise ValueError("Either 'path' or 'uri' must be provided.")

            # Validate the URI scheme before extracting the path
            parsed = urlparse(uri)
            if parsed.scheme not in ("file", ""):
                raise ValueError(
                    _(
                        "Unsupported URI scheme %r for %s. Only 'file' scheme"
                        " or plain paths without a scheme are supported."
                    )
                    % (parsed.scheme, cls.__name__)
                )

            # Extract the path from the URI and convert it to a Path object
            data["path"] = Path(parsed.path)

        return data

    @model_validator(mode="after")
    def _resolve_and_sync(self) -> Self:
        """
        After the model is initialized, resolve the path to an absolute path
        and sync the URI to match. This ensures path and URI are always
        consistent with each other.
        """
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
