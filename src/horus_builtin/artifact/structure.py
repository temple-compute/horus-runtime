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
StructureArtifact: a molecular structure file (PDB, mmCIF, SDF, MOL2, ...).
"""

from typing import ClassVar

from horus_builtin.artifact.file import FileArtifact


class StructureArtifact(FileArtifact):
    """
    A file artifact typed as a molecular structure. Behaves exactly like
    :class:`FileArtifact`; the kind only tells tools how to show it.
    """

    kind: str = "structure"
    kind_name: ClassVar[str] = "Structure"
    kind_description: ClassVar[str] = (
        "A molecular structure file (PDB, mmCIF, SDF, MOL2, ...)."
    )
