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
Shared helper for dumping artifacts with their declared paths.

``BaseArtifact.path`` is resolved to an absolute, CWD-anchored path the
moment the artifact is built, while the original (possibly relative) value is
kept separately on ``declared_path``. Dumping an artifact as-is would
therefore bake the *current process's* CWD into a stored document; restoring
each artifact's ``declared_path`` onto the dumped ``path`` makes the document
say what its author wrote, so a ``to_yaml``/``from_yaml`` round trip keeps
relative paths relative.
"""

from typing import Any

from horus_runtime.core.artifact.base import BaseArtifact


def _restore_declared_paths(
    entries: list[dict[str, Any]], artifacts: list[BaseArtifact]
) -> None:
    """
    Undo the eager CWD resolution ``BaseArtifact`` applies at construction,
    so a dumped document keeps the relative paths its author wrote.

    Args:
        entries: The dumped artifact dicts, aligned positionally with
            *artifacts* (e.g. a dumped task's ``inputs``/``outputs`` lists).
        artifacts: The live artifacts the entries were dumped from.
    """
    for entry, artifact in zip(entries, artifacts, strict=False):
        if artifact.declared_path is not None:
            entry["path"] = str(artifact.declared_path)
