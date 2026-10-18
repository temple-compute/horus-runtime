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
Implementation of the StringArtifact class, which represents a string
value artifact in the Horus runtime.
"""

import re
from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, model_validator

from horus_builtin.artifact.limits import check_count, check_excluded
from horus_builtin.artifact.value import ValueArtifact
from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.i18n import tr as _


class StringArtifact(ValueArtifact[str]):
    """
    Represents a string value artifact.

    The value is materialized as plain text on disk, so a task consuming
    ``$id`` reads a file whose contents are the string as authored (no
    quotes).
    """

    kind: str = "string"
    kind_name: ClassVar[str] = "String"
    kind_description: ClassVar[str] = "A string value artifact."

    value: str = ""

    pattern: str | None = None
    """A Python regex the whole value must match (``None``: anything)."""

    min_length: int | None = Field(default=None, ge=0)
    """The shortest allowed value, in characters (``None``: no minimum)."""

    max_length: int | None = Field(default=None, ge=0)
    """The longest allowed value, in characters (``None``: unbounded)."""

    multiline: bool = False
    """Edit the value in a multi-line box. A UI hint only."""

    exclude: list[str] = Field(default_factory=list)
    """Values that are never allowed, e.g. a placeholder like ``changeme``."""

    @model_validator(mode="after")
    def _check_constraints(self) -> Self:
        check_count(
            self.id,
            len(self.value),
            self.min_length,
            self.max_length,
            _("characters"),
        )
        check_excluded(self.id, self.value, self.exclude)
        if self.pattern is None:
            return self
        try:
            regex = re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(
                _("String artifact '%(id)s' has an invalid pattern: %(e)s")
                % {"id": self.id, "e": exc}
            ) from exc
        if not regex.fullmatch(self.value):
            raise ValueError(
                _(
                    "String artifact '%(id)s': '%(v)s' does not match "
                    "pattern '%(p)s'."
                )
                % {"id": self.id, "v": self.value, "p": self.pattern}
            )
        return self

    def read(self) -> str:
        """
        Read and deserialize the string from the artifact file.
        """
        text = self.path.read_text()
        self._emit_event(ArtifactEventsEnum.READ)
        return text

    def write(self, value: str) -> None:
        """
        Serialize and write the string to the artifact file.
        """
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(value)
        self._emit_event(ArtifactEventsEnum.WRITE)
