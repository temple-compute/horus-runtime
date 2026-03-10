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
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict

HORUS_LOG_ENV_PREFIX = "HORUS_LOG_"

LoggerLevel = Annotated[
    Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    BeforeValidator(lambda v: v.upper() if isinstance(v, str) else v),
]


class HorusLoggerSettings(BaseSettings):
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

    @classmethod
    def setup(cls) -> "Logger":
        """
        Set up the loguru logger with the current settings.
        """
        # Load the configuration from environment variables or defaults
        config = cls()

        # Ensure the log directory exists
        config.log_directory.mkdir(parents=True, exist_ok=True)

        # Remove the default logger and add a new one with our configuration
        logger.remove()
        logger.add(
            sink=f"{config.log_directory}/{config.filename_template}",
            format=config.format,
            level=config.level,
            rotation=config.rotation,
            retention=config.retention,
            compression=config.compression,
            enqueue=True,
        )

        # Add terminal logging as well
        logger.add(
            sink=sys.stderr,
            format=config.format,
            level=config.level,
        )

        return logger


# Instantiate a global logger ready to be used.
horus_logger: "Logger" = HorusLoggerSettings.setup()
