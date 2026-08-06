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
Shared base for value-carrying artifacts.

A value artifact is file-backed like every other artifact, but also carries
an inline, typed ``value`` that is the artifact's authored content. The
workflow graph never distinguishes the two: a ``NumberArtifact`` produced by
one task is transferred to its consumer exactly like a ``NumberArtifact``
root whose value was typed by the user. The only runtime difference is that
a root value artifact materializes itself (``materialize()``) the first time
a task consumes it, because no producer ever wrote its bytes.
"""

from typing import ClassVar

from horus_runtime.core.artifact.base import BaseArtifact


class ValueArtifact[T: object](BaseArtifact[T]):
    """
    A file-backed artifact that carries an inline, typed value.

    Subclasses declare the concrete ``value`` type and the on-disk
    serialization through ``read``/``write`` (exactly like any other
    artifact). ``materialize`` writes ``value`` to ``path`` the first time a
    consumer needs the artifact, so a root value artifact needs no external
    upload to be runnable.
    """

    kind_name: ClassVar[str] = "Value"
    kind_description: ClassVar[str] = "A value-carrying artifact."

    value: T
    """
    The artifact's authored value. Round-trips through ``model_dump`` so a
    workflow document can carry a value that has not been materialized yet.
    """

    def materialize(self) -> None:
        """
        Write ``value`` to ``path`` when no backing file exists yet.

        Idempotent: once the file exists it is left alone, so an uploaded
        blob (or a value already written by a producer task) is never
        clobbered by the declared value.
        """
        if not self.path.exists():
            self.write(self.value)
