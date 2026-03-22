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
Python runtime implementation for horus-runtime.
"""

import sys
from typing import TYPE_CHECKING, Literal

from horus_runtime.core.runtime.base import BaseRuntime

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class PythonRuntime(BaseRuntime):
    """
    Python runtime implementation. Executes a python code snippet.
    """

    kind: Literal["python"] = "python"

    code: str
    """
    The python code to execute.
    """

    formatted_code: str = ""
    """
    The formatted python code after processing any placeholders.
    """

    @property
    def version(self) -> str:
        """
        Returns the current python version.
        """
        return sys.version

    def _setup_runtime(self, task: "BaseTask[PythonRuntime]") -> str:
        """
        Nothing to be done for the PythonRuntime.
        """
        return self.code

    def format_runtime(self, task: "BaseTask[PythonRuntime]") -> str:
        """
        For the PythonRuntime, formatting the runtime simply involves
        returning the code as is, since there are no placeholders to
        replace.
        """
        return self.code
