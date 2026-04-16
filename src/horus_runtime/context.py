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
Runtime initialization, plugin loading, and global context management for
horus-runtime.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field

from horus_runtime.event.base import BaseEvent
from horus_runtime.event.bus import HorusEventBus
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.registry.auto_registry import AutoRegistry

_runtime_ctx: ContextVar["HorusContext"] = ContextVar("horus_runtime_context")


class HorusContextEvent(BaseEvent):
    """
    Base event class for horus-runtime context events.
    """

    event_type: str = "horus_context_event"


class HorusRuntimeReadyEvent(HorusContextEvent):
    """
    Event emitted when the horus runtime is ready.
    """

    event_type: str = "horus_runtime_ready"


class HorusRuntimeWillShutdownEvent(HorusContextEvent):
    """
    Event emitted when the horus runtime is about to shut down.
    """

    event_type: str = "horus_runtime_will_shutdown"


@dataclass(frozen=True)
class HorusContext:
    """
    Main entry point for horus-runtime. Handles initialization, plugin loading,
    and global context management.
    """

    bus: HorusEventBus = field(default_factory=HorusEventBus)
    """
    Event bus for the horus runtime context.
    """

    @staticmethod
    def get_context() -> "HorusContext":
        """
        Get the current HorusContext from the active context.
        """
        try:
            return _runtime_ctx.get()
        except LookupError as e:
            raise RuntimeError(
                _(
                    "HorusContext is not set. Did you forget to call "
                    "HorusContext.boot()?"
                )
            ) from e

    @staticmethod
    def boot() -> "HorusContext":
        """
        Initialize the runtime, load plugins, and set up global context.
        Must be called before using any other horus-runtime features.
        """
        horus_logger.log.info(_("Horus Runtime is starting..."))
        ctx = HorusContext()

        # Register horus-plugins
        AutoRegistry.init_registry()

        # Setup the bus
        ctx.bus.start()

        # Set the context
        _runtime_ctx.set(ctx)

        # Emit a ready event so plugins can react to the runtime being fully
        # initialized
        ctx.bus.emit(
            HorusRuntimeReadyEvent(
                message=_("Horus Runtime ready!"),
            )
        )

        return ctx

    def shutdown(self) -> None:
        """
        Shutdown the runtime gracefully, cleaning up resources and
        stopping transports.
        """
        # Emit a shutdown event before stopping transports so subscribers can
        # react
        self.bus.emit(
            HorusRuntimeWillShutdownEvent(
                message=_("Horus Runtime is shutting down..."),
            )
        )

        # Stop the event bus and all transports
        self.bus.stop()
