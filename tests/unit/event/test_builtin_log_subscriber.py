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
Test module for the built-in LogsSubscriber.
"""

import asyncio
from unittest.mock import patch

import pytest

from horus_builtin.event.log_subscriber import LogsSubscriber
from horus_runtime.events.bus import HorusEventBus
from tests.unit.event.common import _TestEvent


@pytest.mark.unit
class TestLogsSubscriber:
    """
    Test cases for LogsSubscriber setup, handling, and bus registration.
    """

    def test_handle_logs_event_type_at_debug(self) -> None:
        """
        Test that handle() emits a debug log containing the event type.
        """
        sub = LogsSubscriber()
        event = _TestEvent()

        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            debug_msg: str = mock_log.debug.call_args[0][0]

        assert "test.event" in debug_msg

    def test_handle_logs_event_id_at_debug(self) -> None:
        """
        Test that handle() includes the event UUID in the debug log.
        """
        sub = LogsSubscriber()
        event = _TestEvent()

        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            debug_msg: str = mock_log.debug.call_args[0][0]

        assert str(event.event_id) in debug_msg

    def test_handle_logs_event_source_at_debug(self) -> None:
        """
        Test that handle() includes the event source in the debug log.
        """
        sub = LogsSubscriber()
        event = _TestEvent()

        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            debug_msg: str = mock_log.debug.call_args[0][0]

        assert event.source in debug_msg

    def test_handle_logs_message_at_info(self) -> None:
        """
        Test that handle() passes the event message to info().
        """
        sub = LogsSubscriber()
        event = _TestEvent(message="something happened")

        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)

        mock_log.info.assert_called_once_with("something happened")

    def test_handle_logs_none_when_message_absent(self) -> None:
        """
        Test that handle() passes None to info() when the event has no message.
        """
        sub = LogsSubscriber()
        event = _TestEvent()

        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)

        mock_log.info.assert_called_once_with(None)

    def test_handle_calls_debug_before_info(self) -> None:
        """
        Test that debug is called before info — debug carries context, info
        carries payload.
        """
        sub = LogsSubscriber()
        event = _TestEvent(message="ordered")

        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            call_order = [c[0] for c in mock_log.method_calls]

        assert call_order == ["debug", "info"]

    def test_register_on_subscribes_to_all_events(self) -> None:
        """
        Test that register_on() attaches ahandle as a wildcard handler on the
        bus.
        """
        sub = LogsSubscriber()
        bus = HorusEventBus()
        sub.register_on(bus)

        assert sub.ahandle in bus._wildcard_handlers

    def test_ahandle_delegates_to_handle(self) -> None:
        """
        Test that ahandle() falls through to handle() for sync logging.
        """
        sub = LogsSubscriber()
        event = _TestEvent(message="delegated")

        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            asyncio.run(sub.ahandle(event))

        mock_log.info.assert_called_once_with("delegated")
