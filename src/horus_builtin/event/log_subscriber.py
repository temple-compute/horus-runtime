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
Logging subscriber for horus-runtime events.
"""

from typing import Literal

from horus_runtime.events.base import BaseEvent
from horus_runtime.events.subscriber import BaseEventSubscriber
from horus_runtime.logging import horus_logger


class LoguruSubscriber(BaseEventSubscriber):
    """
    A simple event subscriber that logs all events using loguru.
    """

    subscriber_type: Literal["loguru"] = "loguru"

    def setup(self) -> None:
        """
        No setup needed for this subscriber.
        """
        pass

    def handle(self, event: BaseEvent) -> None:
        """
        Handle an incoming event by logging it.
        """
        horus_logger.debug(
            f"Received event: {event.event_type} with ID {event.event_id} "
            f"from {event.source}"
        )
        horus_logger.info(event.message)
