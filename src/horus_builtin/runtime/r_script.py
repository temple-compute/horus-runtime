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
RScriptRuntime: run a local ``.R`` file with ``Rscript`` on the task's target.
"""

from pathlib import Path
from typing import ClassVar

from pydantic import Field

from horus_builtin.runtime.python_script import PythonScriptRuntime
from horus_runtime.i18n import tr as _


class RScriptRuntime(PythonScriptRuntime):
    """
    Ship a local R script to the task's target and run it there.

    Same behaviour as :class:`PythonScriptRuntime`; only the default
    interpreter differs. The ``python`` key is kept so YAML stays
    compatible, and is shown as "Interpreter" through its schema title.
    """

    kind: str = "r_script"
    kind_name: ClassVar[str] = "R Script"
    kind_description: ClassVar[str] = _(
        "Run a local R script file with Rscript on the task's target."
    )

    script: Path
    """Local path to the ``.R`` file to run."""

    python: str = Field("Rscript", title="Interpreter")
    """Interpreter to invoke on the target (override if not ``Rscript``)."""
