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

from typing import Any, ClassVar, TypeVar

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
    batch_key: str

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
    batch_key: str

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
    batch_key: str
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
    batch_key: str
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

    async def ask(
        self,
        interaction: BaseInteraction[T],
        *,
        max_retries: int = 3,
    ) -> T:
        """
        Ask one interaction through the matching renderer.
        """
        ctx = HorusContext.get_context()

        renderer_cls = BaseInteractionRenderer.get_from_registry(
            self, interaction
        )

        if renderer_cls is None:
            ctx.bus.emit(
                InteractionFailedEvent(
                    interaction_kind=interaction.kind,
                    transport_kind=self.kind,
                    batch_key=interaction.batch_key,
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
                batch_key=interaction.batch_key,
            )
        )

        for attempt in range(max_retries):
            raw = await renderer.render(self, interaction)

            try:
                result = await interaction.parse(raw)
            except ValueError:
                if attempt < max_retries - 1:
                    ctx.bus.emit(
                        InteractionRetryEvent(
                            interaction_kind=interaction.kind,
                            transport_kind=self.kind,
                            batch_key=interaction.batch_key,
                            attempt=attempt + 1,
                            max_retries=max_retries,
                        )
                    )
                    continue

                ctx.bus.emit(
                    InteractionFailedEvent(
                        interaction_kind=interaction.kind,
                        transport_kind=self.kind,
                        batch_key=interaction.batch_key,
                        reason=_("All %(retries)d retries exhausted.")
                        % {"retries": max_retries},
                    )
                )
                raise InteractionParseError(
                    interaction.kind, max_retries
                ) from None
            else:
                ctx.bus.emit(
                    InteractionAnsweredEvent(
                        interaction_kind=interaction.kind,
                        transport_kind=self.kind,
                        batch_key=interaction.batch_key,
                    )
                )
                return result

        raise InteractionParseError(interaction.kind, max_retries)
