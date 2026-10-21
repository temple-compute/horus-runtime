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
a root value artifact materializes itself (``materialize()``) when a task
consumes it, because no producer ever wrote its bytes.
"""

from typing import ClassVar

from pydantic import ConfigDict

from horus_runtime.core.artifact.base import BaseArtifact


class ValueArtifact[T: object](BaseArtifact[T]):
    """
    A file-backed artifact that carries an inline, typed value.

    Subclasses declare the concrete ``value`` type and the on-disk
    serialization through ``read``/``write`` (exactly like any other
    artifact). ``materialize`` writes ``value`` to ``path`` when a consumer
    needs the artifact, so a root value artifact needs no external upload to
    be runnable.
    """

    kind_name: ClassVar[str] = "Value"
    kind_description: ClassVar[str] = "A value-carrying artifact."

    # Field docstrings become JSON-schema descriptions: the GUI's limit form
    # shows them as help text.
    model_config = ConfigDict(use_attribute_docstrings=True)

    value: T
    """
    The artifact's authored value. Round-trips through ``model_dump`` so a
    workflow document can carry a value that has not been materialized yet.
    """

    def materialize(self) -> None:
        """
        Write ``value`` to ``path`` unless the file already holds it.

        Only a root value artifact is materialized, and its authored value is
        the source of truth: a file left by an earlier run with another value
        is rewritten, so the consumer's input digest follows the document.
        A file that already holds the value is left untouched.
        """
        if not self.path.exists() or self._stale():
            self.write(self.value)

    def _stale(self) -> bool:
        """Whether the backing file holds something other than ``value``."""
        try:
            return bool(self.read() != self.value)
        except (OSError, ValueError):
            # An unreadable or corrupt file is rewritten from the value.
            return True
