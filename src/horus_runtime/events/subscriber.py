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
from typing import TYPE_CHECKING, ClassVar

from horus_runtime.events.base import BaseEvent
from horus_runtime.registry.auto_registry import AutoRegistry

if TYPE_CHECKING:
    from horus_runtime.events.bus import HorusEventBus


class BaseEventSubscriber(AutoRegistry, entry_point="subscriber"):
    """
    Base class for event subscribers.
    """

    registry_key: ClassVar[str] = "subscriber_type"

    @abstractmethod
    def setup(self) -> None:
        """
        Perform any setup necessary for this subscriber.
        Called once on startup.
        """

    @abstractmethod
    def handle(self, event: BaseEvent) -> None:
        """
        Handle an incoming event. Override this for sync handling.
        """

    async def ahandle(self, event: BaseEvent) -> None:
        """
        Handle an incoming event. Delegates to handle() by default,
        but can be overridden for async handling.
        """
        self.handle(event)

    def register_on(self, bus: "HorusEventBus") -> None:
        """
        Override to control which event types you subscribe to.
        Default: subscribe to everything.
        """
        bus.subscribe_all(self.ahandle)
