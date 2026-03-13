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
Event bus transport base for horus-runtime.
"""

from abc import abstractmethod
from typing import Any, ClassVar

from horus_runtime.event.base import BaseEvent
from horus_runtime.registry.auto_registry import AutoRegistry


class BaseBusTransport(AutoRegistry, entry_point="transport"):
    """
    Defines how events move from emitter to subscribers.
    Local: direct call. Remote: serialize → broker → deserialize.
    """

    registry_key: ClassVar[str] = "transport_type"
    transport_type: Any = None

    @abstractmethod
    async def publish(self, event: BaseEvent) -> None:
        """
        Put the event into the transport.
        """

    @abstractmethod
    async def start(self) -> None:
        """
        Initialize connections, start consumers, etc.
        """

    @abstractmethod
    async def stop(self) -> None:
        """
        Graceful shutdown.
        """
