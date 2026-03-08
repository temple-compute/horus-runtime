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
Runtime initialization, plugin loading, and global context management for
horus-runtime.
"""

from contextvars import ContextVar
from dataclasses import dataclass

_runtime_ctx: ContextVar["HorusRuntime"] = ContextVar("horus_runtime_context")


@dataclass(frozen=True)
class HorusRuntime:
    """
    Main entry point for horus-runtime. Handles initialization, plugin loading,
    and global context management.
    """

    @staticmethod
    def get_context() -> "HorusRuntime":
        """
        Get the current HorusRuntime from the active context.
        """
        return _runtime_ctx.get()

    @staticmethod
    def boot() -> None:
        """
        Initialize the runtime, load plugins, and set up global context.
        Must be called before using any other horus-runtime features.
        """
        _runtime_ctx.set(HorusRuntime())
