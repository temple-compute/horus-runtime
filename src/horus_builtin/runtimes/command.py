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
Command implementation for the runtime.
"""

from typing import TYPE_CHECKING, Literal

from horus_runtime.core.runtime.base import BaseRuntime

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class CommandRuntime(BaseRuntime):
    """
    The CommandRuntime represents a runtime that executes a command directly in
    the local environment. This is the most basic type of runtime, and simply
    runs the specified command as is.
    """

    kind: Literal["command"] = "command"

    command: str
    """
    The command to execute.
    """

    formatted_command: str = ""
    """
    The formatted command after processing any placeholders.
    """

    def _setup_runtime(self, task: "BaseTask") -> str:
        """
        Nothing to be done for the CommandRuntime
        """

        return self.command

    def format_runtime(self, task: "BaseTask") -> str:
        fmt = super().format_runtime(task)

        self.formatted_command = fmt

        return fmt
