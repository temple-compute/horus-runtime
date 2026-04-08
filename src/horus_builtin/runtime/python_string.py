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

from typing import TYPE_CHECKING

from horus_runtime.core.runtime.base import BaseRuntime

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class PythonCodeStringRuntime(BaseRuntime[str]):
    """
    Executes a Python code snippet.
    """

    kind: str = "python"

    code: str
    """
    The Python code to execute.
    """

    def setup_runtime(self, _: "BaseTask") -> str:
        """
        For the PythonCodeStringRuntime, setting up the runtime simply involves
        returning the code as is.
        """
        return self.code
