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
from pathlib import Path, PurePosixPath
from typing import ClassVar

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.iterable import IterableArtifact
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.i18n import tr as _


class FolderArtifact(BaseArtifact[Path], IterableArtifact):
    """
    Represents a local folder artifact.
    """

    kind: str = "folder"
    kind_name: ClassVar[str] = "Folder"
    kind_description: ClassVar[str] = (
        "A directory artifact containing multiple files."
    )

    async def items(self, target: BaseTarget) -> list[BaseArtifact]:
        """
        One item per child of the folder, sorted by name.

        Each item points at the child's own path *on the target* (zero
        copying), so a consumer can hand it straight to a task running there.
        Read strictly through *target*'s channels, so enumeration works for
        remote targets too.
        """
        entries = await target.list_dir(target.path_on_target(self))
        return [
            (FolderArtifact if entry.is_dir else FileArtifact)(
                id=f"{self.id}:{entry.name}", path=Path(entry.path)
            )
            for entry in sorted(entries, key=lambda e: e.name)
        ]

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

        Compressed at gzip's fastest level: packaging sits on the critical
        path (a task's outputs are packed before it completes) and level 6
        took about twice as long for a few percent smaller tarballs. Still
        plain gzip, so every unpacker reads it. A ``tar`` without
        ``--use-compress-program`` (busybox) falls back to ``czf``.
        """
        q_pkg, q_src = shlex.quote(pkg), shlex.quote(src)
        return (
            f"tar --use-compress-program='gzip -1' -cf {q_pkg} -C {q_src} . "
            f"2>/dev/null || tar czf {q_pkg} -C {q_src} ."
        )

    def unpack_command(self, pkg: str, dest: str) -> str | None:
        """
        Extract the gzipped tarball *pkg* into the directory *dest*.

        Several consumers of one folder input unpack it onto the same *dest*
        while earlier consumers still read it, so *dest* is never emptied in
        place:

        - The package's sha256 is kept in a marker next to *dest*. When
          *dest* exists and the marker matches, nothing is touched.
        - Otherwise the package is extracted into a temporary sibling, the
          old *dest* is renamed aside, the new tree renamed in, and the old
          one removed. A re-materialized folder never mixes with stale
          contents.
        """
        d = PurePosixPath(dest)
        q_dest, q_pkg = shlex.quote(dest), shlex.quote(pkg)
        q_parent = shlex.quote(str(d.parent))
        q_mark = shlex.quote(str(d.parent / f".{d.name}.horus-sha256"))
        q_tmp = shlex.quote(f"{dest}.horus-tmp") + "$$"
        q_old = shlex.quote(f"{dest}.horus-old") + "$$"
        q_nested = shlex.quote(f"{dest}/{d.name}.horus-tmp") + "$$"
        # shortcut: the marker trusts that tasks do not edit their input
        # folders in place; a consumer that does would keep the edited copy.
        # ponytail: between the two renames *dest* is briefly absent; only a
        # changed package swaps, so readers of the same content never see it.
        return (
            "h=sha256sum; command -v sha256sum >/dev/null || h='shasum -a 256'"
            f"; s=$($h {q_pkg} 2>/dev/null | cut -d' ' -f1)"
            f'; if [ -d {q_dest} ] && [ -n "$s" ] '
            f'&& [ "$(cat {q_mark} 2>/dev/null)" = "$s" ]; then :; else '
            f"mkdir -p {q_parent} && rm -rf {q_tmp} && mkdir {q_tmp} "
            f"&& tar xzf {q_pkg} -C {q_tmp} "
            f"&& {{ [ ! -e {q_dest} ] || mv {q_dest} {q_old}; }} "
            # A dest made by a concurrent unpack in between makes mv nest
            # the temp tree inside it; drop that copy and keep the other.
            f"&& mv {q_tmp} {q_dest} && rm -rf {q_nested} {q_old} "
            f'&& {{ [ -z "$s" ] || echo "$s" > {q_mark}; }}; fi'
        )
