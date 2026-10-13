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

import time
from collections import defaultdict
from concurrent.futures import Future
from dataclasses import dataclass, field
from functools import partial

from horus_runtime.event.async_loop import BusAsyncLoopThread
from horus_runtime.event.base import BaseEvent
from horus_runtime.event.subscriber import BaseEventSubscriber
from horus_runtime.event.transport import BaseBusTransport
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger


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

    _handlers: defaultdict[
        type[BaseEvent], list[BaseEventSubscriber[BaseEvent]]
    ] = field(default_factory=lambda: defaultdict(list))
    """
    Mapping from event type to list of handler callables.
    """

    _started: bool = False
    """
    Whether the event bus has been started. Used to prevent multiple starts.
    """

    _pending: set[Future[None]] = field(default_factory=set)
    """
    Publishes submitted to the transports that have not finished yet.
    """

    def add_transport(self, transport: BaseBusTransport) -> None:
        """
        Add a transport mechanism to the event bus.
        """
        self._transports.append(transport)

    def subscribe(self, subscriber: BaseEventSubscriber[BaseEvent]) -> None:
        """
        Subscribe a handler to events it declares interest in.
        If no events are declared, subscribes to all events.
        """
        for event_type in subscriber.events:
            horus_logger.log.debug(
                _(
                    "Subscribing handler %(subscriber)s to event "
                    "type %(event_type)s"
                )
                % {
                    "subscriber": subscriber.__class__.__name__,
                    "event_type": event_type.__name__,
                }
            )
            self._handlers[event_type].append(subscriber)

    def emit(self, event: BaseEvent) -> None:
        """
        Emit an event to the bus.
        """
        # Publishing is fire-and-forget: a transport may do network I/O, and
        # waiting on it here would stall the caller's event loop (every
        # concurrent task) for one round-trip per event. The bus thread runs
        # publishes in submission order; `flush` waits for the backlog.
        for transport in self._transports:
            future = self._event_loop_thread.submit(transport.publish(event))
            self._pending.add(future)
            future.add_done_callback(partial(self._settle, event=event))

        # In-process dispatch to handlers
        self._dispatch(event)

    def _settle(self, future: Future[None], event: BaseEvent) -> None:
        """
        Forget a finished publish, logging it if it failed.
        """
        self._pending.discard(future)
        if future.cancelled() or future.exception() is None:
            return
        horus_logger.log.error(
            _("Error publishing event %(event)s: %(error)s")
            % {"event": event, "error": str(future.exception())}
        )

    def flush(self, timeout: float | None = None) -> None:
        """
        Block until every event emitted so far, including any emitted while
        flushing, has been published (or has failed, which `_settle` logs).
        Gives up after *timeout* seconds, so a hung transport cannot hold
        the caller forever.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while waiting := [f for f in list(self._pending) if not f.done()]:
            for future in waiting:
                left = (
                    None if deadline is None else deadline - time.monotonic()
                )
                try:
                    future.result(
                        timeout=None if left is None else max(left, 0)
                    )
                except TimeoutError:
                    horus_logger.log.warning(
                        _("Gave up flushing %(n)d unpublished event(s).")
                        % {"n": len(self._pending)}
                    )
                    return
                except Exception:  # already logged by _settle
                    pass

    def _dispatch(self, event: BaseEvent) -> None:
        """
        Dispatch an event to all relevant handlers based on its type.
        """
        for event_type, handlers in self._handlers.items():
            if isinstance(event, event_type):
                for handler in handlers:
                    handler.handle(event)

    def start(self) -> None:
        """
        Initializes transport and subscribers from the registry.
        """
        if self._started:
            horus_logger.log.warning(_("Event bus is already started."))
            return

        horus_logger.log.debug(
            _("Initializing transport and subscribers from the registry.")
        )

        # Register transport buses
        transport_futures: list[Future[None]] = []
        for transport_cls in BaseBusTransport.registry.values():
            transport = transport_cls()

            # Start the transport
            horus_logger.log.debug(
                _("Starting transport: %(transport_type)s")
                % {"transport_type": transport.transport_type}
            )
            future = self._event_loop_thread.submit(transport.start())
            transport_futures.append(future)
            self.add_transport(transport)

        # Wait for all transports to start
        for future in transport_futures:
            future.result()

        # Register event subscribers on the bus
        for subscriber_cls in BaseEventSubscriber.registry.values():
            subscriber = subscriber_cls()

            horus_logger.log.debug(
                _("Subscribing handler: %(subscriber_cls)s")
                % {"subscriber_cls": subscriber_cls.__name__}
            )

            subscriber.setup()
            self.subscribe(subscriber)

        # Flag the bus as started to prevent multiple starts
        self._started = True

    def stop(self) -> None:
        """
        Stop the event bus and its transport.
        """
        # Deliver what is still queued before the transports go away
        self.flush()

        # Schedule transport shutdown on the event loop thread
        for transport in self._transports:
            horus_logger.log.debug(
                _("Stopping transport: %(transport_type)s")
                % {"transport_type": transport.transport_type}
            )

            future = self._event_loop_thread.submit(transport.stop())
            try:
                future.result()  # Wait for the transport to stop
            except Exception as e:
                horus_logger.log.error(
                    _("Error stopping transport %(transport_type)s: %(error)s")
                    % {
                        "transport_type": transport.transport_type,
                        "error": str(e),
                    }
                )

        # Stop the event loop thread after all transports have stopped
        self._event_loop_thread.stop()
