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
Core interaction primitives.
"""

from abc import abstractmethod
from typing import Any, ClassVar

from horus_runtime.registry.auto_registry import AutoRegistry


class BaseInteraction[T: Any = Any](AutoRegistry, entry_point="interaction"):
    """
    Defines an interaction prompt plus its validation logic.
    """

    registry_key: ClassVar[str] = "kind"

    kind: str
    value_key: str
    title: str | None = None
    prompt: str | None = None
    description: str | None = None
    default: T | None = None
    value: T | None = None

    @abstractmethod
    async def parse(self, value: object) -> T:
        """
        Validate and coerce a raw renderer value.
        """
