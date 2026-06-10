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
Settings and configuration for Horus Runtime.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class HorusRuntimeSettings(BaseSettings):
    """
    Settings for Horus Runtime.
    """

    model_config = SettingsConfigDict(env_prefix="HORUS_RUNTIME_")

    SIDE_ARTIFACTS_DIR_ENV: str = "HORUS_SIDE_ARTIFACTS_DIR"


runtime_settings = HorusRuntimeSettings()

__all__ = ["runtime_settings"]
