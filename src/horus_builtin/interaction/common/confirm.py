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
Common interaction.
"""

from horus_runtime.core.interaction.base import BaseInteraction


class ConfirmInteraction(BaseInteraction[bool]):
    """
    Boolean confirmation input.
    """

    kind: str = "confirm"

    default: bool | None = None
    confirm_label: str | None = None
    cancel_label: str | None = None

    async def parse(self, value: object) -> bool:
        """
        Parse common truthy and falsy text values.
        """
        if isinstance(value, bool):
            return value

        if value in (None, "") and self.default is not None:
            return self.default

        normalized = str(value).strip().lower()
        if normalized in {"y", "yes", "true", "1"}:
            return True
        if normalized in {"n", "no", "false", "0"}:
            return False

        raise ValueError(f"Cannot parse confirmation value: {value!r}")
