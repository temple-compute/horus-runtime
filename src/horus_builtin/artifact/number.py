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
Implementation of the NumberArtifact class, which represents a numeric
value artifact in the Horus runtime.
"""

import json
from pathlib import Path
from typing import ClassVar, Self, cast

from pydantic import Field, model_validator

from horus_builtin.artifact.limits import check_excluded, check_range
from horus_builtin.artifact.value import ValueArtifact
from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.i18n import tr as _


class NumberArtifact(ValueArtifact[int | float]):
    """
    Represents a numeric value (integer or float) artifact.

    The value is materialized as a JSON number on disk, so a task consuming
    ``$id`` reads a file whose contents are the bare number.
    """

    kind: str = "number"
    kind_name: ClassVar[str] = "Number"
    kind_description: ClassVar[str] = "A numeric value artifact."

    value: int | float = 0

    min: int | float | None = None
    """The smallest allowed value (``None``: unbounded)."""

    max: int | float | None = None
    """The largest allowed value (``None``: unbounded)."""

    step: int | float | None = Field(default=None, gt=0)
    """
    The allowed increment, counted from ``min`` (or 0): ``step: 1`` with an
    integer ``min`` allows only integers. ``None`` allows any value.
    """

    exclude: list[int | float] = Field(default_factory=list)
    """Values that are never allowed, even inside the range."""

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if "value" not in self.model_fields_set and self.min is not None:
            # An unset value (e.g. stripped from a published release) starts
            # in range instead of failing on the 0 default.
            self.value = self.min
        check_range(self.name or self.id, self.value, self.min, self.max)
        if self.step is not None:
            n = (self.value - (self.min or 0)) / self.step
            # Tolerance so 0.3 sits on a 0.1 grid despite float rounding.
            if abs(n - round(n)) > 1e-9 * max(1.0, abs(n)):
                raise ValueError(
                    _("%(name)s must be a multiple of %(step)s from %(base)s.")
                    % {
                        "name": self.name or self.id,
                        "step": self.step,
                        "base": self.min or 0,
                    }
                )
        check_excluded(self.name or self.id, self.value, self.exclude)
        return self

    def read(self) -> int | float:
        """
        Read and deserialize the number from the artifact file.
        """
        with open(self.path) as f:
            value = json.load(f)
        self._emit_event(ArtifactEventsEnum.READ)
        return cast(int | float, value)

    def write(self, value: int | float) -> None:
        """
        Serialize and write the number to the artifact file.
        """
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(value, f)
        self._emit_event(ArtifactEventsEnum.WRITE)
