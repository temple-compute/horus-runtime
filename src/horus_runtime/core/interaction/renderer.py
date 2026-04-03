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
from typing import TYPE_CHECKING, Any, ClassVar, Self

from horus_runtime.registry.auto_registry import AutoRegistry

if TYPE_CHECKING:
    from horus_runtime.core.interaction.base import BaseInteraction
    from horus_runtime.core.interaction.transport import (
        BaseInteractionTransport,
    )
# Type aliases for renderer class attributes.
HandlesTransportType = type["BaseInteractionTransport"]
HandlesInteractionType = type["BaseInteraction[Any]"]


class BaseInteractionRenderer[
    T: "BaseInteractionTransport" = "BaseInteractionTransport",
    I: "BaseInteraction[Any]" = "BaseInteraction[Any]",
](AutoRegistry, entry_point="interaction_renderer"):
    """
    Maps one interaction type to one transport type. Defines how to render
    an interaction and collect a raw answer from the user.
    """

    registry_key: ClassVar[str] = "render_key"
    render_key: str | None = None
    handles_transport: ClassVar[type[T]]
    handles_interaction: ClassVar[type[I]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """
        Automatically intiialize the render_key based on the transport and
        interaction types.
        """
        # Only set the render_key for fully defined subclasses,
        # not for intermediate ones.
        if not hasattr(cls, "handles_transport") or not hasattr(
            cls, "handles_interaction"
        ):
            return

        # `kind` is a Pydantic model field (not a ClassVar), so it is not
        # accessible as a class attribute. Read the registered default value
        # via model_fields instead.
        transport_fields = getattr(cls.handles_transport, "model_fields", {})
        interaction_fields = getattr(
            cls.handles_interaction, "model_fields", {}
        )

        if "kind" not in transport_fields or "kind" not in interaction_fields:
            return

        transport_kind = transport_fields["kind"].default
        interaction_kind = interaction_fields["kind"].default

        if not transport_kind or not interaction_kind:
            return

        # Automatically set the render_key based on the transport and
        # interaction kinds.
        cls.render_key = f"{transport_kind}:{interaction_kind}"

        super().__init_subclass__(**kwargs)

    @classmethod
    def get_from_registry(
        cls,
        transport: T,
        interaction: I,
    ) -> type[Self] | None:
        """
        Look up the renderer that handles the given transport/interaction pair.
        """
        return cls.registry.get(f"{transport.kind}:{interaction.kind}")

    @abstractmethod
    async def render(
        self,
        transport: T,
        interaction: I,
    ) -> object:
        """
        Render the interaction and return the raw answer.
        """
