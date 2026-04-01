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

from horus_runtime.event.base import BaseEvent
from horus_runtime.event.subscriber import BaseEventSubscriber
from horus_runtime.logging import horus_logger


class LogsSubscriber(BaseEventSubscriber):
    """
    A simple event subscriber that logs all events using loguru.
    """

    subscriber_type: str = "loguru"

    def setup(self) -> None:
        """
        No setup needed for this subscriber.
        """
        pass

    def handle(self, event: BaseEvent) -> None:
        """
        Handle an incoming event by logging it using loguru's bind.
        """
        # Secure the message by escaping any potential markup characters
        safe_message = (event.message or "").replace("<", r"\<")

        horus_logger.log.opt(colors=True).log(
            event.level,
            "<cyan>[{source}]</cyan> <yellow>[{event_type}]</yellow> "
            "{safe_message}",
            source=event.source,
            event_type=event.event_type,
            safe_message=safe_message,
        )
