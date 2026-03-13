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


@dataclass(frozen=True)
class HorusContext:
    """
    Main entry point for horus-runtime. Handles initialization, plugin loading,
    and global context management.
    """

    bus: HorusEventBus = field(default_factory=HorusEventBus)

    @staticmethod
    def get_context() -> "HorusContext":
        """
        Get the current HorusContext from the active context.
        """
        return _runtime_ctx.get()

    @staticmethod
    def boot() -> "HorusContext":
        """
        Initialize the runtime, load plugins, and set up global context.
        Must be called before using any other horus-runtime features.
        """
        horus_logger.info(_("Horus Runtime is starting..."))
        ctx = HorusContext()

        # Register horus-plugins
        AutoRegistry.init_registry()

        # Setup the bus
        ctx.bus.start()

        # Set the context
        _runtime_ctx.set(ctx)

        # Send a test event to see if loguru event handler is working
        ctx.bus.emit(
            HorusContextEvent(
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
            HorusContextEvent(
                message=_("Horus Runtime is shutting down..."),
            )
        )

        # Stop the event bus and all transports
        self.bus.stop()
