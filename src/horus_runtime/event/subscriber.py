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
Base event subscriber class for horus-runtime.
"""

from abc import abstractmethod
from typing import ClassVar

from horus_runtime.event.base import BaseEvent
from horus_runtime.registry.auto_registry import AutoRegistry

EventFilterType = tuple[type[BaseEvent], ...]


class BaseEventSubscriber[E: BaseEvent = BaseEvent](
    AutoRegistry, entry_point="subscriber"
):
    """
    Base class for event subscribers.
    """

    registry_key: ClassVar[str] = "subscriber_type"

    subscriber_type: str | None = None
    """
    The 'subscriber_type' field is used to identify the specific type
    of subscriber.
    """

    events: ClassVar[EventFilterType] = (BaseEvent,)
    """
    Which event types this subscriber is interested in.
    If None, the subscriber will receive all events.
    """

    @abstractmethod
    def setup(self) -> None:
        """
        Perform any setup necessary for this subscriber.
        Called once on startup.
        """

    @abstractmethod
    def handle(self, event: E) -> None:
        """
        Handle an incoming event. Override this for sync handling.
        """
