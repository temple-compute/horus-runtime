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
Interaction renderers. This defines how renderers are implemented and
registered.
"""

from abc import abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from horus_runtime.registry.auto_registry import AutoRegistry
from horus_runtime.registry.auto_registry_product import AutoRegistryProduct

if TYPE_CHECKING:
    from horus_runtime.core.interaction.base import BaseInteraction
    from horus_runtime.core.interaction.transport import (
        BaseInteractionTransport,
    )

# Type aliases for the renderer class attributes that identify which
# transport and interaction types a renderer handles.
HandlesTransportType = type["BaseInteractionTransport"]
HandlesInteractionType = type["BaseInteraction[Any]"]


class BaseInteractionRenderer[
    T: "BaseInteractionTransport" = "BaseInteractionTransport",
    I: "BaseInteraction[Any]" = "BaseInteraction[Any]",
](AutoRegistryProduct, AutoRegistry, entry_point="interaction_renderer"):
    """
    Maps one interaction type to one transport type. Defines how to render
    an interaction and collect a raw answer from the user.
    """

    registry_key: ClassVar[str] = (
        "render_key:handles_transport.handles_interaction"
    )
    render_key: str | None = None
    handles_transport: ClassVar[HandlesTransportType]
    handles_interaction: ClassVar[HandlesInteractionType]

    @abstractmethod
    async def render(
        self,
        transport: T,
        interaction: I,
    ) -> object:
        """
        Render the interaction and return the raw answer.
        """
