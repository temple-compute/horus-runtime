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

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from horus_builtin.event.log_subscriber import LogsSubscriber
from horus_runtime.event.bus import HorusEventBus
from tests.unit.event.common import _TestEvent


@pytest.mark.unit
class TestLogsSubscriber:
    """
    Test cases for LogsSubscriber setup, handling, and bus registration.
    """

    def _call_args(
        self, mock_log: MagicMock
    ) -> tuple[tuple[Any], dict[Any, Any]]:
        """
        Extract (args, kwargs) from the opt().log() call.
        """
        opt_instance = mock_log.opt.return_value
        args, kwargs = opt_instance.log.call_args
        return args, kwargs

    def test_handle_logs_correct_level(self) -> None:
        """
        Test that handle() calls log() with the event's level.
        """
        sub = LogsSubscriber()
        event = _TestEvent()
        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            args, _ = self._call_args(mock_log)
            assert args[0] == event.level

    def test_handle_logs_event_type(self) -> None:
        """
        Test that handle() passes the event type as a kwarg to log().
        """
        sub = LogsSubscriber()
        event = _TestEvent()
        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            _, kwargs = self._call_args(mock_log)
            assert kwargs["event_type"] == event.event_type

    def test_handle_logs_event_source(self) -> None:
        """
        Test that handle() passes the event source as a kwarg to log().
        """
        sub = LogsSubscriber()
        event = _TestEvent()
        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            _, kwargs = self._call_args(mock_log)
            assert kwargs["source"] == event.source

    def test_handle_logs_event_message(self) -> None:
        """
        Test that handle() passes the event message as safe_message to log().
        """
        sub = LogsSubscriber()
        event = _TestEvent(message="something happened")
        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            _, kwargs = self._call_args(mock_log)
            assert kwargs["safe_message"] == "something happened"

    def test_handle_escapes_markup_in_message(self) -> None:
        """
        Test that handle() escapes '<' characters to prevent loguru markup
        injection.
        """
        sub = LogsSubscriber()
        event = _TestEvent(message="<danger>")
        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            _, kwargs = self._call_args(mock_log)

            assert r"\<" in kwargs["safe_message"]

    def test_handle_none_message_defaults_to_empty(self) -> None:
        """
        Test that handle() converts a None message to an empty string.
        """
        sub = LogsSubscriber()
        event = _TestEvent()
        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            _, kwargs = self._call_args(mock_log)
            assert kwargs["safe_message"] == ""

    def test_handle_calls_opt_with_colors(self) -> None:
        """
        Test that handle() enables color rendering via opt(colors=True).
        """
        sub = LogsSubscriber()
        event = _TestEvent()
        with patch(
            "horus_builtin.event.log_subscriber.horus_logger"
        ) as mock_log:
            sub.handle(event)
            mock_log.opt.assert_called_once_with(colors=True)

    def test_register_on_subscribes_to_all_events(self) -> None:
        """
        Test that subscribing LogsSubscriber to the bus adds it to the
        wildcard handlers the bus.
        """
        sub = LogsSubscriber()
        bus = HorusEventBus()
        bus.subscribe(sub)
        assert sub in bus._wildcard_handlers
