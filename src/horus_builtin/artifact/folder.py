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
import os
import shutil
import tempfile
from pathlib import Path

from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.core.artifact.base import BaseArtifact


class FolderArtifact(BaseArtifact[Path]):
    """
    Represents a local folder artifact.
    """

    kind: str = "folder"

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

    def read(self) -> Path:
        """
        Return the canonical directory path for this artifact.
        """
        self._emit_event(ArtifactEventsEnum.READ)
        return self.path

    def write(self, value: Path) -> None:
        """
        Materialize this folder artifact from another local directory.

        WARNING: THIS WILL OVERWRITE ANY EXISTING CONTENT AT THE ARTIFACT PATH.
        """
        source_path = Path(value).resolve()
        if not source_path.is_dir():
            raise ValueError(f"Expected directory path, got {source_path}")

        if self.path.exists():
            shutil.rmtree(self.path)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_path, self.path)
        self._emit_event(ArtifactEventsEnum.WRITE)

    def package(self) -> Path:
        """
        Archive the folder into a zip file and return the archive path.
        """
        if not self.exists():
            raise FileNotFoundError(self.path)

        # Create a temporary file to be used as the archive path.
        # shutil.make_archive requires a base name without extension,
        # so we create a temp file and then remove it after archiving.
        fd, arch_p = tempfile.mkstemp()
        os.close(fd)
        archive_path = Path(arch_p)

        shutil.make_archive(
            base_name=str(archive_path),
            format="zip",
            root_dir=self.path,
        )

        # shutil.make_archive adds .zip, so remove the temp
        # file and use the generated archive
        archive_file = archive_path.with_suffix(".zip")
        if archive_path.exists():
            archive_path.unlink()

        self._emit_event(ArtifactEventsEnum.PACKAGE)
        return archive_file

    def unpackage(self, package_path: Path) -> None:
        """
        Extract a packaged folder archive into the canonical directory path.
        """
        package_path = Path(package_path).resolve()

        if self.path.exists():
            shutil.rmtree(self.path)

        self.path.mkdir(parents=True, exist_ok=True)
        shutil.unpack_archive(
            filename=str(package_path),
            extract_dir=str(self.path),
        )
        self._emit_event(ArtifactEventsEnum.UNPACKAGE)

    def delete(self) -> None:
        """
        Deletes the artifact from its location by deleting the folder at the
        specified path.
        """
        if self.exists():
            shutil.rmtree(self.path)
            self._emit_event(ArtifactEventsEnum.DELETE)
