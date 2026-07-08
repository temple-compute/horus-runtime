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

import shlex
import shutil
from pathlib import Path
from typing import ClassVar

from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.i18n import tr as _


class FolderArtifact(BaseArtifact[Path]):
    """
    Represents a local folder artifact.
    """

    kind: str = "folder"
    kind_name: ClassVar[str] = "Folder"
    kind_description: ClassVar[str] = (
        "A directory artifact containing multiple files."
    )

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
            raise ValueError(
                _("Expected directory path, got %(source_path)s")
                % {"source_path": source_path}
            )

        if self.path.exists():
            shutil.rmtree(self.path)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_path, self.path)
        self._emit_event(ArtifactEventsEnum.WRITE)

    def pack_command(self, src: str, pkg: str) -> str | None:
        """
        Archive the *contents* of the folder at *src* into the gzipped tarball
        *pkg*.

        Uses ``tar -C <src> .`` so the archive holds the directory's contents
        rooted at ``.`` (not the directory name itself); the matching
        :meth:`unpack_command` then extracts straight into the destination
        directory without nesting.
        """
        return f"tar czf {shlex.quote(pkg)} -C {shlex.quote(src)} ."

    def unpack_command(self, pkg: str, dest: str) -> str | None:
        """
        Extract the gzipped tarball *pkg* into the directory *dest*.

        The destination is recreated fresh so a re-materialized folder never
        mixes with stale contents.
        """
        q_dest = shlex.quote(dest)
        return (
            f"rm -rf {q_dest} && mkdir -p {q_dest} && "
            f"tar xzf {shlex.quote(pkg)} -C {q_dest}"
        )
