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

from string import Template
from typing import TYPE_CHECKING, ClassVar

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
        Substitute ``$input`` / ``${output}`` placeholders with artifact paths.

        Uses ``string.Template`` (not ``str.format``) so Python's ``{}`` —
        dict/set literals, f-strings, comprehensions — is left untouched.
        Placeholders are artifact ids mapping to their on-target path;
        unknown ``$name`` references are left as-is via ``safe_substitute``.
        """
        paths = {
            a.id: str(task.target.path_on_target(a))
            for a in (*task.inputs, *task.outputs)
        }
        return Template(self.code).safe_substitute(paths)
