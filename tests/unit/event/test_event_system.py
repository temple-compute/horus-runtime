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

import datetime
import threading
import uuid
from typing import ClassVar, Literal

import pytest
from pydantic import Field, ValidationError

from horus_runtime.event.base import BaseEvent
from horus_runtime.event.bus import HorusEventBus
from horus_runtime.event.transport import BaseBusTransport
from tests.unit.event.common import (
    _CollectingAllSubscriber,
    _CollectingOtherSubscriber,
    _CollectingTestSubscriber,
    _OtherEvent,
    _TestEvent,
)


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
        assert event.timestamp.tzinfo == datetime.UTC

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
        sub = _CollectingTestSubscriber()

        bus.subscribe(sub)

        event = _TestEvent()
        bus._dispatch(event)

        assert len(sub.received) == 1
        assert sub.received[0] is event

    def test_subscribe_ignores_non_matching_event_type(self) -> None:
        """
        Test that a handler registered for a different type does not fire.
        """
        bus = HorusEventBus()
        sub = _CollectingOtherSubscriber()
        bus.subscribe(sub)

        bus._dispatch(_TestEvent())

        assert len(sub.received) == 0

    def test_subscribe_all_receives_every_event(self) -> None:
        """
        Test that a wildcard handler registered via subscribe_all() receives
        all dispatched events.
        """
        bus = HorusEventBus()
        sub = _CollectingAllSubscriber()
        bus.subscribe(sub)

        bus._dispatch(_TestEvent())
        bus._dispatch(_OtherEvent())

        runs = 2

        assert len(sub.received) == runs
        assert sub.received[0].event_type == "test.event"
        assert sub.received[1].event_type == "other.event"

    def test_both_specific_and_wildcard_handlers_fire(self) -> None:
        """
        Test that both a type-specific handler and a wildcard handler fire for
        the same event.
        """
        bus = HorusEventBus()
        specific = _CollectingTestSubscriber()
        wildcard = _CollectingAllSubscriber()
        bus.subscribe(specific)
        bus.subscribe(wildcard)

        specific_events = [_TestEvent(), _TestEvent()]
        other_events = [_OtherEvent(), _OtherEvent()]
        all_events = specific_events + other_events
        for event in all_events:
            bus._dispatch(event)

        assert len(specific.received) == len(specific_events)
        assert len(wildcard.received) == len(all_events)


@pytest.mark.unit
class TestHorusEventBusEmit:
    """
    Test cases for HorusEventBus emit paths (sync and async).
    """

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
        bus.emit(_TestEvent())

    def test_submit_and_forget_async_publish(self) -> None:
        """
        Test that emitting an event submits the publish coroutine to the async
        loop without awaiting it, allowing the emit call to return immediately.
        """
        bus = HorusEventBus()

        class _RecordingTransport(BaseBusTransport):
            transport_type: Literal["recording"] = "recording"
            published_events: list[BaseEvent] = Field(default_factory=list)
            event_published: ClassVar[threading.Event] = threading.Event()

            async def publish(self, event: BaseEvent) -> None:
                self.published_events.append(event)
                self.event_published.set()

            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                pass

        transport = _RecordingTransport()
        bus.add_transport(transport)

        event = _TestEvent()
        bus.emit(event)

        # Wait deterministically for the event to be published.
        published = transport.event_published.wait(timeout=1.0)
        assert published
        assert len(transport.published_events) == 1
        assert transport.published_events[0] is event
