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
PythonCodeStringRuntime implementation for horus-runtime.
"""

from typing import TYPE_CHECKING, ClassVar

from horus_builtin.runtime.command import format_command
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class PythonCodeStringRuntime(BaseRuntime[str]):
    """
    Executes a Python code snippet.
    """

    kind: str = "python"
    kind_name: ClassVar[str] = "Python Code String"
    kind_description: ClassVar[str] = _(
        "A runtime that executes a Python code snippet provided as a string."
    )

    code: str
    """
    The Python code to execute.
    """

    async def _setup_runtime(self, task: "BaseTask") -> str:
        """
        For the PythonCodeStringRuntime, setting up the runtime simply involves
        returning the code as is.
        """
        # Format palceholders as if it was a command.
        # This allows users to use placeholders like {input} or {output} in
        # their code string.
        return format_command(self.code, task)
