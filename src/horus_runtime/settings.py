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

    MAX_SIDE_ARTIFACT_BYTES: int = 100 * 1024 * 1024
    """
    Maximum size (bytes) of a single side-artifact file collected back to
    the orchestrator. Files above this are skipped with a warning; large
    data should be declared as task inputs/outputs instead.
    """

    STREAM_QUEUE_MAXSIZE: int = 100
    """
    Maximum number of lines to buffer in the stdout/stderr stream queue before
    backpressure is applied to the target channel.
    """

    ALLOW_PYTHON_CONDITIONS: bool = True
    """
    Whether ``python`` edge conditions and ``branch_router`` deciders may run.
    Hosts that do not trust the workflow author with code in the runtime
    process set ``HORUS_RUNTIME_ALLOW_PYTHON_CONDITIONS=false``.
    """

    ALLOW_IN_PROCESS: bool = True
    """
    Whether workflow code may run inside the runtime process at all: in-process
    executors (``in_process = True``), ``python`` edge conditions and
    ``branch_router`` deciders. Checked when each one runs, so it also covers
    map clones, loop iterations and subworkflows. Hosts that isolate workflow
    code set ``HORUS_RUNTIME_ALLOW_IN_PROCESS=false``.
    """

    def python_callables_allowed(self) -> bool:
        """True when workflow-named Python callables may run in-process."""
        return self.ALLOW_IN_PROCESS and self.ALLOW_PYTHON_CONDITIONS


runtime_settings = HorusRuntimeSettings()

__all__ = ["runtime_settings"]
