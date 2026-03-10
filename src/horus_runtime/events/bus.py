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
import concurrent.futures
import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from horus_runtime.events.base import BaseEvent
from horus_runtime.events.subscriber import BaseEventSubscriber
from horus_runtime.events.transport import BaseBusTransport


@dataclass
class HorusEventBus:
    """
    Base event bus class. Defines the interface for event buses.
    """

    _transports: list[BaseBusTransport] = field(default_factory=list)
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

    def add_transport(self, transport: BaseBusTransport) -> None:
        """
        Add a transport mechanism to the event bus.
        """
        self._transports.append(transport)

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
        await self._dispatch(event)
        await asyncio.gather(
            *(t.publish(event) for t in self._transports),
            # one failing transport doesn't kill others
            return_exceptions=True,
        )

    def emit(self, event: BaseEvent) -> None:
        """
        Sync emit. Safe to call from sync task code.

        When called from inside a running event loop (e.g. from a Jupyter
        notebook or an async runner), ``create_task`` would only schedule the
        coroutine and it would not run until the current synchronous call
        stack returns control to the loop, meaning all events would appear
        after all task executions finish.  To guarantee immediate dispatch
        even in that case, we run the coroutine in a worker thread that owns
        its own event loop.
        """
        try:
            asyncio.get_running_loop()
            # A loop is running in this thread but we cannot await here.
            # Dispatch in a dedicated thread so the call is blocking and
            # completes before we return.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, self.aemit(event)).result()
        except RuntimeError:
            # No running loop, run directly.
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
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, handler, event)

    async def start(self) -> None:
        """
        Start the event bus and its transport.
        """
        await asyncio.gather(*(t.start() for t in self._transports))

    async def stop(self) -> None:
        """
        Stop the event bus and its transport.
        """
        await asyncio.gather(*(t.stop() for t in self._transports))

    def setup_bus(self) -> None:
        """
        Initializes transport and subscribers from the registry.
        """
        # Register transport buses
        for transport_cls in BaseBusTransport.registry.values():
            transport = transport_cls()
            self.add_transport(transport)

        # Register event subscribers on the bus
        for subscriber_cls in BaseEventSubscriber.registry.values():
            subscriber = subscriber_cls()
            subscriber.setup()
            subscriber.register_on(self)
