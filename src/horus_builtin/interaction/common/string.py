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
String interaction and renderers.
"""

from horus_runtime.core.interaction.base import BaseInteraction


class StringInteraction(BaseInteraction[str]):
    """
    Free-form text input.
    """

    kind: str = "string"

    placeholder: str | None = None
    strip: bool = True

    async def parse(self, value: object) -> str:
        """
        Coerce input to text, optionally applying the default.
        """
        if value in (None, "") and self.default is not None:
            return self.default

        result = str(value)
        return result.strip() if self.strip else result
