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
File interaction.
"""

from pathlib import Path

from horus_builtin.artifact.file import FileArtifact
from horus_runtime.core.interaction.base import BaseInteraction
from horus_runtime.i18n import tr as _


class FileInteraction(BaseInteraction[FileArtifact]):
    """
    Ask for a file path on the local filesystem.
    """

    kind: str = "file"
    accept: list[str] | None = None
    must_exist: bool = True

    async def parse(self, value: object) -> FileArtifact:
        """
        Validate file existence and extension constraints.
        """
        if value in (None, "") and self.default is not None:
            value = self.default

        if isinstance(value, FileArtifact):
            path = value.path
        else:
            path = Path(str(value))

        if self.must_exist and not path.exists():
            raise ValueError(_("File not found: %(path)s") % {"path": path})

        if self.accept and path.suffix not in self.accept:
            raise ValueError(
                _("Expected one of %(accepted)s, got %(actual)s")
                % {
                    "accepted": self.accept,
                    "actual": path.suffix,
                }
            )

        return FileArtifact(path=path)
