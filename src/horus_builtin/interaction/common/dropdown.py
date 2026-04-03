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
Dropdown interaction.
"""

from pydantic import Field

from horus_runtime.core.interaction.base import BaseInteraction


class DropdownInteraction(BaseInteraction[str]):
    """
    Single-selection dropdown input.
    """

    kind: str = "dropdown"

    options: list[str] = Field(default_factory=list)
    """
    Options to present in the dropdown. If empty, any value is accepted.
    """

    async def parse(self, value: object) -> str:
        """
        Validate that the selected value is one of the available options.
        """
        if value in (None, "") and self.default is not None:
            return self.default

        result = str(value)
        if self.options and result not in self.options:
            raise ValueError(
                f"Invalid selection: {result}. "
                f"Must be one of: {', '.join(self.options)}"
            )
        return result
