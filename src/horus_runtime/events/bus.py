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
Event bus base for horus-runtime.
"""

import asyncio
import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from horus_runtime.events.base import BaseEvent
from horus_runtime.events.transport import BaseBusTransport


@dataclass
class BaseEventBus:
    """
    Base event bus class. Defines the interface for event buses.
    """

    _transport: BaseBusTransport | None = None
    """
    The transport mechanism for the event bus.
    """

    _handlers: defaultdict[str, list[Any]] = field(
        default_factory=lambda: defaultdict(list)
    )
    """
    Mapping from event type to list of handler callables.
    """

    _wildcard_handlers: list[Any] = field(default_factory=list)
    """
    Handlers that receive all events regardless of type.
    """

    _tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    """
    Set of running tasks for cleanup on shutdown.
    """

    def subscribe(self, event_type: str, handler: Any) -> None:
        """
        Subscribe a handler to a specific event type.
        """
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: Any) -> None:
        """
        Subscribe a handler to all events.
        """
        self._wildcard_handlers.append(handler)

    async def aemit(self, event: BaseEvent) -> None:
        """
        Async emit. Use from async contexts.
        """
        await self._transport.publish(event) if self._transport else None
        await self._dispatch(event)

    def emit(self, event: BaseEvent) -> None:
        """
        Sync emit. Safe to call from sync task code.
        Schedules on the running loop if one exists,
        otherwise runs synchronously.
        """
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self.aemit(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except RuntimeError:
            # No running loop, run sync dispatch directly
            asyncio.run(self.aemit(event))

    async def _dispatch(self, event: BaseEvent) -> None:
        handlers = (
            self._handlers.get(event.event_type, []) + self._wildcard_handlers
        )
        for handler in handlers:
            if inspect.iscoroutinefunction(handler):
                await handler(event)
            else:
                # Sync handler — run in executor to avoid blocking loop
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, handler, event)

    async def start(self) -> None:
        """
        Start the event bus and its transport.
        """
        await self._transport.start() if self._transport else None

    async def stop(self) -> None:
        """
        Stop the event bus and its transport.
        """
        await self._transport.stop() if self._transport else None
