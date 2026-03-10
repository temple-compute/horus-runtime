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
Test module for horus-runtime event system (BaseEvent, HorusEventBus,
BaseBusTransport).
"""

import asyncio
import datetime
import uuid
from typing import Literal

import pytest
from pydantic import ValidationError

from horus_runtime.events.base import BaseEvent
from horus_runtime.events.bus import HorusEventBus
from horus_runtime.events.transport import BaseBusTransport
from tests.unit.event.common import _TestEvent


@pytest.mark.unit
class TestBaseEvent:
    """
    Test cases for BaseEvent field defaults and immutability.
    """

    def test_event_id_is_uuid(self) -> None:
        """
        Test that event_id is automatically generated as a UUID.
        """
        event = _TestEvent()
        assert isinstance(event.event_id, uuid.UUID)

    def test_event_id_is_unique_per_instance(self) -> None:
        """
        Test that two events created in sequence receive distinct IDs.
        """
        assert _TestEvent().event_id != _TestEvent().event_id

    def test_timestamp_is_utc(self) -> None:
        """
        Test that the auto-generated timestamp carries UTC timezone info.
        """
        event = _TestEvent()
        assert event.timestamp.tzinfo == datetime.timezone.utc

    def test_source_is_inferred(self) -> None:
        """
        Test that source is inferred from the call stack and is non-empty.
        """
        event = _TestEvent()
        assert isinstance(event.source, str)
        assert event.source != ""

    def test_message_defaults_to_none(self) -> None:
        """
        Test that message is None when not provided.
        """
        assert _TestEvent().message is None

    def test_message_can_be_set_at_construction(self) -> None:
        """
        Test that message is stored correctly when provided.
        """
        event = _TestEvent(message="hello")
        assert event.message == "hello"

    def test_event_is_immutable(self) -> None:
        """
        Test that mutating a field on a frozen event raises an exception.
        """
        event = _TestEvent()
        with pytest.raises(ValidationError):
            event.message = "mutated"  # type: ignore[misc]

    def test_explicit_event_id_is_preserved(self) -> None:
        """
        Test that a caller-supplied event_id is not overwritten.
        """
        fixed = uuid.uuid4()
        event = _TestEvent(event_id=fixed)
        assert event.event_id == fixed


@pytest.mark.unit
class TestHorusEventBusSubscriptions:
    """
    Test cases for HorusEventBus subscription and dispatch routing.
    """

    def test_subscribe_routes_matching_event_type(self) -> None:
        """
        Test that a handler registered with subscribe() receives events of the
        matching type.
        """
        bus = HorusEventBus()
        received: list[BaseEvent] = []
        bus.subscribe("test.event", received.append)

        event = _TestEvent()
        asyncio.run(bus._dispatch(event))

        assert len(received) == 1
        assert received[0] is event

    def test_subscribe_ignores_non_matching_event_type(self) -> None:
        """
        Test that a handler registered for a different type does not fire.
        """
        bus = HorusEventBus()
        received: list[BaseEvent] = []
        bus.subscribe("other.event", received.append)

        asyncio.run(bus._dispatch(_TestEvent()))

        assert len(received) == 0

    def test_subscribe_all_receives_every_event(self) -> None:
        """
        Test that a wildcard handler registered via subscribe_all() receives
        all dispatched events.
        """
        bus = HorusEventBus()
        received: list[BaseEvent] = []
        bus.subscribe_all(received.append)

        asyncio.run(bus._dispatch(_TestEvent()))
        asyncio.run(bus._dispatch(_TestEvent()))

        runs = 2

        assert len(received) == runs

    def test_both_specific_and_wildcard_handlers_fire(self) -> None:
        """
        Test that both a type-specific handler and a wildcard handler fire for
        the same event.
        """
        bus = HorusEventBus()
        specific: list[BaseEvent] = []
        wildcard: list[BaseEvent] = []

        bus.subscribe("test.event", specific.append)
        bus.subscribe_all(wildcard.append)

        asyncio.run(bus._dispatch(_TestEvent()))

        assert len(specific) == 1
        assert len(wildcard) == 1

    def test_async_handler_is_awaited(self) -> None:
        """
        Test that coroutine handlers are awaited rather than skipped.
        """
        bus = HorusEventBus()
        received: list[BaseEvent] = []

        async def _handler(event: BaseEvent) -> None:
            received.append(event)

        bus.subscribe_all(_handler)
        asyncio.run(bus._dispatch(_TestEvent()))

        assert len(received) == 1

    def test_multiple_handlers_for_same_type_all_called(self) -> None:
        """
        Test that all handlers registered for the same event type are invoked.
        """
        bus = HorusEventBus()
        calls: list[int] = []

        bus.subscribe("test.event", lambda e: calls.append(1))
        bus.subscribe("test.event", lambda e: calls.append(2))

        asyncio.run(bus._dispatch(_TestEvent()))

        assert calls == [1, 2]


@pytest.mark.unit
class TestHorusEventBusEmit:
    """
    Test cases for HorusEventBus emit paths (sync and async).
    """

    def test_aemit_dispatches_to_handlers(self) -> None:
        """
        Test that aemit() triggers registered handlers.
        """
        bus = HorusEventBus()
        received: list[BaseEvent] = []
        bus.subscribe_all(received.append)

        event = _TestEvent()
        asyncio.run(bus.aemit(event))

        assert len(received) == 1
        assert received[0] is event

    def test_aemit_calls_transport_publish(self) -> None:
        """
        Test that aemit() calls publish() on every registered transport.
        """
        published: list[BaseEvent] = []

        class _CapturingTransport(BaseBusTransport):
            transport_type: Literal["capturing"] = "capturing"

            async def publish(self, event: BaseEvent) -> None:
                published.append(event)

            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                pass

        bus = HorusEventBus()
        bus.add_transport(_CapturingTransport())

        event = _TestEvent()
        asyncio.run(bus.aemit(event))

        assert event in published

    def test_failing_transport_does_not_propagate_exception(self) -> None:
        """
        Test that a transport raising during publish does not surface to the
        caller — other transports and handlers must still run.
        """

        class _FailingTransport(BaseBusTransport):
            transport_type: Literal["failing"] = "failing"

            async def publish(self, event: BaseEvent) -> None:
                raise RuntimeError("transport down")

            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                pass

        bus = HorusEventBus()
        bus.add_transport(_FailingTransport())

        # Must not raise.
        asyncio.run(bus.aemit(_TestEvent()))

    def test_sync_emit_runs_when_no_loop(self) -> None:
        """
        Test that sync emit() dispatches the event when called outside any
        running event loop.
        """
        bus = HorusEventBus()
        received: list[BaseEvent] = []
        bus.subscribe_all(received.append)

        event = _TestEvent()
        bus.emit(event)  # no running loop — falls back to asyncio.run()

        assert len(received) == 1

    def test_sync_emit_dispatches_immediately_on_running_loop(self) -> None:
        """
        Test that sync emit() dispatches the event immediately even when called
        from inside a running event loop, without requiring a yield point.
        """
        bus = HorusEventBus()
        received: list[BaseEvent] = []
        bus.subscribe_all(received.append)

        async def _runner() -> None:
            event = _TestEvent()
            bus.emit(event)
            # Event must already be dispatched — no yield needed.
            assert len(received) == 1

        asyncio.run(_runner())
        assert len(received) == 1
