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

from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.core.artifact.base import BaseArtifact


class FileArtifact(BaseArtifact[str]):
    """
    Represents a local file artifact.
    """

    kind: str = "file"

    def read(self) -> str:
        """
        Read and deserialize the contents of the file artifact.

        Returns:
            The full text content of the file.
        """
        txt = self.path.read_text()
        self._emit_event(ArtifactEventsEnum.READ)
        return txt

    def write(self, value: str) -> None:
        """
        Write text content to the file artifact path.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(value)
        self._emit_event(ArtifactEventsEnum.WRITE)
