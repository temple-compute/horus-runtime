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

from typing import TYPE_CHECKING, ClassVar

from horus_builtin.runtime.substitution import substitute
from horus_runtime.context import HorusContext
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.runtime.events import RuntimeEvent
from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class CommandRuntime(BaseRuntime[str]):
    """
    The CommandRuntime represents a runtime that executes a command directly in
    the local environment. This is the most basic type of runtime, and simply
    runs the specified command as is.
    """

    kind: str = "command"
    kind_name: ClassVar[str] = "Command"
    kind_description: ClassVar[str] = _(
        "Execute a command directly in the local environment."
    )

    command: str
    """
    The command to execute. Supports ``$id`` / ``${id}`` / ``${id.attr}`` /
    ``${task.attr}`` placeholders (``string.Template`` syntax). ``{}`` passes
    through untouched.
    """

    formatted_command: str = ""
    """
    The formatted command after processing any placeholders.
    """

    async def _setup_runtime(self, task: "BaseTask") -> str:
        """
        Render ``$``/``${}`` placeholders in the command against *task*'s
        artifacts and task namespace, then emit a ``RuntimeEvent``.
        """
        fmt = substitute(self.command, task)

        self.formatted_command = fmt

        ctx = HorusContext.get_context()

        ctx.bus.emit(
            RuntimeEvent(
                runtime_kind=self.kind,
                task_id=task.id,
                details={"formatted_command": fmt},
            )
        )

        return fmt
