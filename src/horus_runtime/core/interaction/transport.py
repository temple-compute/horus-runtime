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
Core interaction transport and related events.
"""

from typing import Any, ClassVar, TypeVar, final

from pydantic import model_validator

from horus_runtime.context import HorusContext
from horus_runtime.core.interaction.base import BaseInteraction
from horus_runtime.core.interaction.exceptions import (
    InteractionParseError,
    RendererNotFoundError,
)
from horus_runtime.core.interaction.renderer import (
    BaseInteractionRenderer,
)
from horus_runtime.event.base import BaseEvent
from horus_runtime.i18n import tr as _
from horus_runtime.logging import LoggerLevel
from horus_runtime.middleware.interaction import (
    InteractionMiddleware,
    InteractionMiddlewareContext,
)
from horus_runtime.registry.auto_registry import AutoRegistry

# Typevar allows us to maintain type information about the expected
# return type of an interaction, which is lost if we just use `object`
# or `Any`. For example, and StringInteraction expects a `str` answer,
# so using `BaseInteraction[str]` allows us to preserve that information in
# the transport and renderer layers, enabling better type checking and
# autocompletion.
T = TypeVar("T")


class InteractionAskedEvent(BaseEvent):
    """
    Emitted when an interaction is presented to the user.
    """

    event_type: str = "interaction_asked"

    interaction_kind: str
    transport_kind: str
    renderer_key: str
    value_key: str

    @model_validator(mode="before")
    @classmethod
    def set_message(
        cls,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """Set event message from interaction metadata."""
        values["message"] = _(
            "Interaction '%(interaction)s' asked via "
            "transport '%(transport)s' using "
            "renderer '%(renderer)s'."
        ) % {
            "interaction": values.get("interaction_kind", ""),
            "transport": values.get("transport_kind", ""),
            "renderer": values.get("renderer_key", ""),
        }
        return values


class InteractionAnsweredEvent(BaseEvent):
    """
    Emitted when an interaction is successfully answered and parsed.
    """

    event_type: str = "interaction_answered"

    interaction_kind: str
    transport_kind: str
    value_key: str

    @model_validator(mode="before")
    @classmethod
    def set_message(
        cls,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """Set event message from interaction metadata."""
        values["message"] = _(
            "Interaction '%(interaction)s' answered successfully."
        ) % {
            "interaction": values.get("interaction_kind", ""),
        }
        return values


class InteractionRetryEvent(BaseEvent):
    """
    Emitted when an interaction parse fails and a retry is attempted.
    """

    event_type: str = "interaction_retry"
    level: LoggerLevel = "WARNING"

    interaction_kind: str
    transport_kind: str
    value_key: str
    attempt: int
    max_retries: int

    @model_validator(mode="before")
    @classmethod
    def set_message(
        cls,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """Set event message from interaction metadata."""
        values["message"] = _(
            "Interaction '%(interaction)s' parse failed."
            " Retry %(attempt)d of %(max_retries)d."
        ) % {
            "interaction": values.get("interaction_kind", ""),
            "attempt": values.get("attempt", 0),
            "max_retries": values.get("max_retries", 0),
        }
        return values


class InteractionFailedEvent(BaseEvent):
    """
    Emitted when an interaction fails permanently (all retries exhausted or
    renderer not found).
    """

    event_type: str = "interaction_failed"
    level: LoggerLevel = "ERROR"

    interaction_kind: str
    transport_kind: str
    value_key: str
    reason: str

    @model_validator(mode="before")
    @classmethod
    def set_message(
        cls,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """Set event message from interaction metadata."""
        values["message"] = _(
            "Interaction '%(interaction)s' failed: %(reason)s"
        ) % {
            "interaction": values.get("interaction_kind", ""),
            "reason": values.get("reason", ""),
        }
        return values


class BaseInteractionTransport(
    AutoRegistry, entry_point="interaction_transport"
):
    """
    Interaction transport. This class defines how interactions are collected
    from the user.
    """

    registry_key: ClassVar[str] = "kind"
    add_to_registry: ClassVar[bool] = False
    kind: str

    @final
    async def ask(
        self,
        interaction: BaseInteraction[T],
        *,
        max_retries: int = 3,
    ) -> T:
        """
        Ask one interaction through the matching renderer.
        """
        # Validate max_retries before attempting to get the renderer
        if max_retries < 1:
            raise ValueError(_("max_retries must be at least 1."))

        ctx = HorusContext.get_context()

        # Obtain the appropriate renderer for this interaction and transport
        # For example, a StringInteraction asked through a
        # CLIInteractionTransport would look for a renderer that handles both
        # StringInteraction and CLIInteractionTransport, such as
        # CLIStringRenderer.
        renderer_cls = BaseInteractionRenderer.get_from_registry(
            self, interaction
        )

        # Fail if no renderer is found, since we won't be able to ask the user
        if renderer_cls is None:
            ctx.bus.emit(
                InteractionFailedEvent(
                    interaction_kind=interaction.kind,
                    transport_kind=self.kind,
                    value_key=interaction.value_key,
                    reason=_(
                        "No renderer registered for"
                        " %(transport)s:%(interaction)s"
                    )
                    % {
                        "transport": self.kind,
                        "interaction": interaction.kind,
                    },
                )
            )
            raise RendererNotFoundError(self.kind, interaction.kind)

        renderer = renderer_cls()

        ctx.bus.emit(
            InteractionAskedEvent(
                interaction_kind=interaction.kind,
                transport_kind=self.kind,
                renderer_key=renderer.render_key or "",
                value_key=interaction.value_key,
            )
        )

        # Try to render and parse the interaction, with retries on parse
        # failure (up to max_retries). Middleware is entered on each attempt,
        # allowing for things like retry notifications or dynamic adjustments
        # to the transport or renderer between attempts.
        for attempt in range(max_retries):
            middleware_context = InteractionMiddlewareContext(
                transport=self,
                interaction=interaction,
                renderer=renderer,
                attempt=attempt,
            )

            async def render_current(
                current_context: InteractionMiddlewareContext = (
                    middleware_context
                ),
            ) -> object:
                """
                Render using the current middleware-mutated context.
                """
                return await current_context.renderer.render(
                    current_context.transport,
                    current_context.interaction,
                )

            raw = await InteractionMiddleware.call_with_middleware(
                middleware_context,
                render_current,
            )

            try:
                result = await interaction.parse(raw)
            except ValueError:
                if attempt < max_retries - 1:
                    ctx.bus.emit(
                        InteractionRetryEvent(
                            interaction_kind=interaction.kind,
                            transport_kind=self.kind,
                            value_key=interaction.value_key,
                            attempt=attempt + 1,
                            max_retries=max_retries,
                        )
                    )
                    continue

                ctx.bus.emit(
                    InteractionFailedEvent(
                        interaction_kind=interaction.kind,
                        transport_kind=self.kind,
                        value_key=interaction.value_key,
                        reason=_("All %(retries)d retries exhausted.")
                        % {"retries": max_retries},
                    )
                )
                raise InteractionParseError(
                    interaction.kind, max_retries
                ) from None

            ctx.bus.emit(
                InteractionAnsweredEvent(
                    interaction_kind=interaction.kind,
                    transport_kind=self.kind,
                    value_key=interaction.value_key,
                )
            )
            return result

        # This point should not be reachable due to the raise in the except
        # block, but is required to satisfy the type checker that a T is always
        # returned or an exception is raised
        raise InteractionParseError(interaction.kind, max_retries)
