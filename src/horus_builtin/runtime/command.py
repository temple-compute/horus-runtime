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

from typing import TYPE_CHECKING, TypeVar

from horus_runtime.core.runtime.base import BaseRuntime

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


BR = TypeVar("BR", bound="CommandRuntime")


# Create a namespace object to allow for attribute-style access to task
# variables and inputs in the command formatting. This allows users to
# write commands like "echo {task.input1.path}" in the workflow yaml
class _TaskNamespace:
    def __init__(self, task: "BaseTask[BR]"):
        for name, value in vars(task).items():
            setattr(self, name, value)
        for name, artifact in task.inputs.items():
            setattr(self, name, artifact)
        for name, artifact in task.outputs.items():
            setattr(self, name, artifact)


class CommandRuntime(BaseRuntime[str]):
    """
    The CommandRuntime represents a runtime that executes a command directly in
    the local environment. This is the most basic type of runtime, and simply
    runs the specified command as is.
    """

    kind: str = "command"

    command: str
    """
    The command to execute.
    """

    formatted_command: str = ""
    """
    The formatted command after processing any placeholders.
    """

    def setup_runtime(self, task: "BaseTask[CommandRuntime]") -> str:
        """
        For the CommandRuntime, setting up the runtime simply involves
        returning the command as is, since there are no placeholders to
        replace.
        """
        fmt_kwargs = {
            "task": _TaskNamespace(task),
            **task.inputs,
            **task.outputs,
            **task.variables,
        }

        fmt = self.command.format(**fmt_kwargs)

        self.formatted_command = fmt

        return fmt
