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
Loguru setup for horus-runtime.
"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger, Message

from pydantic import BeforeValidator, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

HORUS_LOG_ENV_PREFIX = "HORUS_LOG_"

LoggerLevel = Annotated[
    Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    BeforeValidator(lambda v: v.upper() if isinstance(v, str) else v),
]


class HorusLogger(BaseSettings):
    """
    Configuration for the loguru logger.
    """

    model_config = SettingsConfigDict(
        env_prefix=HORUS_LOG_ENV_PREFIX,
    )

    format: str = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level:<8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    level: LoggerLevel = "INFO"
    log_directory: Path = Path("logs")
    rotation: str = "10 MB"
    retention: str = "7 days"
    compression: str | None = None
    filename_template: str = "log_{time:YYYY-MM-DD}.log"

    #: Loguru handler id of the terminal sink, tracked so it can be swapped
    #: (e.g. the live TUI redirects it to a panel) without touching the file
    #: sink. ``None`` until :meth:`setup` runs.
    _terminal_sink_id: int | None = PrivateAttr(default=None)

    @property
    def log(self) -> "Logger":
        """
        Get the configured loguru logger instance.
        """
        return logger

    def setup(self, level: LoggerLevel | None = None) -> None:
        """
        Set up the loguru logger with the current settings.
        """
        # Load the configuration from environment variables or defaults
        self.level = level or self.level

        # Ensure the log directory exists
        self.log_directory.mkdir(parents=True, exist_ok=True)

        # Remove the default logger and add a new one with our configuration
        logger.remove()
        logger.add(
            sink=f"{self.log_directory}/{self.filename_template}",
            format=self.format,
            level=self.level,
            rotation=self.rotation,
            retention=self.retention,
            compression=self.compression,
            enqueue=True,
        )

        # Add terminal logging as well, tracking its id so it can be swapped.
        self._terminal_sink_id = logger.add(
            sink=sys.stdout,
            format=self.format,
            level=self.level,
        )

    def redirect_terminal(self, sink: "Callable[[Message], None]") -> None:
        """
        Replace the terminal (stdout) sink with *sink*, leaving the file sink
        intact. Used by the live TUI to capture log records into a panel.
        """
        if self._terminal_sink_id is not None:
            logger.remove(self._terminal_sink_id)
        self._terminal_sink_id = logger.add(sink=sink, level=self.level)

    def restore_terminal(self) -> None:
        """
        Restore stdout terminal logging after :meth:`redirect_terminal`.
        """
        if self._terminal_sink_id is not None:
            logger.remove(self._terminal_sink_id)
        self._terminal_sink_id = logger.add(
            sink=sys.stdout,
            format=self.format,
            level=self.level,
        )

    def set_level(self, level: LoggerLevel) -> None:
        """
        Set the logging level at runtime.
        """
        self.setup(level=level)  # Re-setup to apply the new level


# Instantiate a global logger ready to be used.
horus_logger: "HorusLogger" = HorusLogger()
horus_logger.setup()
