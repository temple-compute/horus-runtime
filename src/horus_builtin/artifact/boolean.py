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
Implementation of the BooleanArtifact class, which represents a boolean
value artifact in the Horus runtime.
"""

import json
from pathlib import Path
from typing import ClassVar, Self

from pydantic import TypeAdapter, model_validator

from horus_builtin.artifact.options import Option
from horus_builtin.artifact.value import ValueArtifact
from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.i18n import tr as _

# ``bool`` first: it is what a plain boolean holds, and the UI reads the
# first type of the ``value`` schema to pick its control (a switch).
BooleanValue = bool | str | int | float

_BOOL = TypeAdapter(bool)


class BooleanArtifact(ValueArtifact[BooleanValue]):
    """
    Represents a boolean (``true``/``false``) value artifact.

    The value is materialized as a JSON boolean on disk, so a task consuming
    ``$id`` reads a file whose contents are ``true`` or ``false``.

    Optionally, ``on`` and ``off`` map the two states to the values the task
    receives instead (a "Use GPU" toggle that renders ``$device`` as ``gpu``
    or ``cpu``). ``value`` then holds the mapped value, never the toggle
    state, so ``$id``, the file on disk and ``read()`` all agree on what the
    task sees; the toggle is on when ``value == on.value``. A bare
    ``true``/``false`` given to a mapped boolean (authored, written, or left
    on disk by a producer that wrote the state) is translated to its mapped
    value.
    """

    kind: str = "boolean"
    kind_name: ClassVar[str] = "Boolean"
    kind_description: ClassVar[str] = "A boolean value artifact."

    value: BooleanValue = False

    on: Option | None = None
    """What the task receives when the toggle is on (``None``: ``true``)."""

    off: Option | None = None
    """What the task receives when the toggle is off (``None``: ``false``)."""

    @model_validator(mode="after")
    def _check_mapping(self) -> Self:
        if self.on is None and self.off is None:
            # A plain boolean coerces exactly as the old ``value: bool`` did.
            self.value = _BOOL.validate_python(self.value)
            return self
        if self.on is None or self.off is None:
            raise ValueError(
                _("A boolean maps both 'on' and 'off', or neither.")
            )
        if isinstance(self.on.value, bool) or isinstance(self.off.value, bool):
            # ``true``/``false`` already name the state, so mapping to them
            # is ambiguous (an inverted mapping would flip on every reload).
            raise ValueError(_("Mapped boolean values cannot be true/false."))
        if self.on.value == self.off.value:
            raise ValueError(_("'on' and 'off' must map to different values."))
        self.value = self._mapped(self.value)
        if self.value not in (self.on.value, self.off.value):
            raise ValueError(
                _(
                    "Value %(value)r is neither the 'on' value %(on)r nor "
                    "the 'off' value %(off)r."
                )
                % {
                    "value": self.value,
                    "on": self.on.value,
                    "off": self.off.value,
                }
            )
        return self

    def _mapped(self, value: BooleanValue) -> BooleanValue:
        """Translate a toggle state to the value the task receives."""
        if self.on is None or self.off is None or not isinstance(value, bool):
            return value
        return self.on.value if value else self.off.value

    def read(self) -> BooleanValue:
        """
        Read and deserialize the value from the artifact file.

        For a mapped boolean this is the mapped value, even when a producer
        wrote the bare state (``true``/``false``).
        """
        with open(self.path) as f:
            value = json.load(f)
        self._emit_event(ArtifactEventsEnum.READ)
        return self._mapped(value)

    def write(self, value: BooleanValue) -> None:
        """
        Serialize and write the value to the artifact file.

        A mapped boolean accepts either the state or the mapped value and
        always writes the mapped value.
        """
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._mapped(value), f)
        self._emit_event(ArtifactEventsEnum.WRITE)
