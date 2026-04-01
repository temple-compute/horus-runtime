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
Common TestEvent.
"""

from pydantic import Field

from horus_runtime.event.base import BaseEvent
from horus_runtime.event.subscriber import BaseEventSubscriber


class _TestEvent(BaseEvent):
    """
    Concrete event for testing purposes.
    """

    event_type: str = "test.event"


class _OtherEvent(BaseEvent):
    event_type: str = "other.event"


class _CollectingAllSubscriber(BaseEventSubscriber):
    """
    Concrete subscriber that records every event passed to handle().
    Accepts an optional events filter; defaults to wildcard (no filter).
    """

    subscriber_type: str = "collecting"
    received: list[BaseEvent] = Field(default_factory=list)

    def setup(self) -> None:
        pass

    def handle(self, event: BaseEvent) -> None:
        self.received.append(event)


class _CollectingTestSubscriber(BaseEventSubscriber):
    """
    Concrete subscriber that records every event passed to handle().
    Only accepts events of type _TestEvent.
    """

    subscriber_type: str = "collecting_test"
    received: list[BaseEvent] = Field(default_factory=list)

    events = (_TestEvent,)

    def setup(self) -> None:
        pass

    def handle(self, event: BaseEvent) -> None:
        self.received.append(event)


class _CollectingOtherSubscriber(BaseEventSubscriber):
    """
    Concrete subscriber that records every event passed to handle().
    Only accepts events of type _OtherEvent.
    """

    subscriber_type: str = "collecting_other"
    received: list[BaseEvent] = Field(default_factory=list)

    events = (_OtherEvent,)

    def setup(self) -> None:
        pass

    def handle(self, event: BaseEvent) -> None:
        self.received.append(event)
