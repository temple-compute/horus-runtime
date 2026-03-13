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
from collections import defaultdict
from dataclasses import dataclass, field

from horus_runtime.event.async_loop import BusAsyncLoopThread
from horus_runtime.event.base import BaseEvent
from horus_runtime.event.subscriber import BaseEventSubscriber
from horus_runtime.event.transport import BaseBusTransport


@dataclass
class HorusEventBus:
    """
    Base event bus class. Defines the interface for event buses.
    """

    _event_loop_thread: BusAsyncLoopThread = field(
        default_factory=BusAsyncLoopThread
    )
    """
    Thread running the asyncio event loop for async transport operations.
    """

    _transports: list[BaseBusTransport] = field(default_factory=list)
    """
    The transport mechanism for the event bus.
    """

    _handlers: defaultdict[type[BaseEvent], list[BaseEventSubscriber]] = field(
        default_factory=lambda: defaultdict(list)
    )
    """
    Mapping from event type to list of handler callables.
    """

    _wildcard_handlers: list[BaseEventSubscriber] = field(default_factory=list)
    """
    Handlers that receive all events regardless of type.
    """

    def add_transport(self, transport: BaseBusTransport) -> None:
        """
        Add a transport mechanism to the event bus.
        """
        self._transports.append(transport)

    def subscribe(self, subscriber: BaseEventSubscriber) -> None:
        """
        Subscribe a handler to events it declares interest in.
        If no events are declared, subscribes to all events.
        """
        if not subscriber.events:
            self._wildcard_handlers.append(subscriber)
        else:
            for event_type in subscriber.events:
                self._handlers[event_type].append(subscriber)

    def emit(self, event: BaseEvent) -> None:
        """
        Emit an event to the bus.
        """
        for transport in self._transports:
            self._event_loop_thread.submit(transport.publish(event))

        # In-process dispatch to handlers
        self._dispatch(event)

    def _dispatch(self, event: BaseEvent) -> None:
        """
        Dispatch an event to all relevant handlers based on its type.
        """
        for handler in self._handlers[type(event)]:
            handler.handle(event)

        for handler in self._wildcard_handlers:
            handler.handle(event)

    def stop(self) -> None:
        """
        Stop the event bus and its transport.
        """

        async def _stop_transports() -> None:
            await asyncio.gather(*(t.stop() for t in self._transports))

        asyncio.run(_stop_transports())

    def start(self) -> None:
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
            self.subscribe(subscriber)
