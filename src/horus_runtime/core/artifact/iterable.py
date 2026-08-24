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
The iterable-artifact contract: artifacts that can enumerate themselves into
a deterministic list of ``BaseArtifact`` items.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from horus_runtime.core.artifact.base import BaseArtifact

if TYPE_CHECKING:
    from horus_runtime.core.target.base import BaseTarget


class ArtifactIterationError(Exception):
    """Raised when an artifact cannot be enumerated into items."""


class IterableArtifact(ABC):
    """
    Mixin for artifacts whose contents enumerate into a deterministic,
    ordered list of :class:`ArtifactItem` elements.
    """

    @abstractmethod
    async def items(self, target: "BaseTarget") -> list[BaseArtifact]:
        """
        Enumerate the artifact's contents into a list of ``BaseArtifact``.
        """
