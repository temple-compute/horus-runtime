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
Unit tests for logger settings module.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from horus_runtime.logging import HorusLogger


@pytest.mark.unit
class TestHorusLoggerSettings:
    """
    Test cases for HorusLoggerSettings module.
    """

    def test_default_level(self) -> None:
        """
        Test that default log level is INFO.
        """
        config = HorusLogger()
        assert config.level == "INFO"

    def test_default_log_directory(self) -> None:
        """
        Test that default log directory is 'logs'.
        """
        config = HorusLogger()
        assert config.log_directory == Path("logs")

    def test_env_prefix_overrides_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Test that HORUS_LOG_ env prefix correctly overrides log level.
        """
        monkeypatch.setenv("HORUS_LOG_level", "DEBUG")
        config = HorusLogger()
        assert config.level == "DEBUG"

    def test_invalid_level_raises(self) -> None:
        """
        Test that an invalid log level raises a validation error.
        """
        with pytest.raises(ValidationError):
            HorusLogger(level="INVALID")  # type: ignore[arg-type]

    @patch("horus_runtime.logging.logger")
    def test_setup_removes_default_logger(
        self, mock_logger: MagicMock
    ) -> None:
        """
        Test that setup() calls logger.remove() to clear default handlers.
        """
        instance = HorusLogger()
        instance.setup()

        mock_logger.remove.assert_called_once()
