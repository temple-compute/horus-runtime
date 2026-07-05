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
PythonScriptRuntime: run a local ``.py`` file on whatever target the task uses,
without the caller managing any remote paths.
"""

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.substitution import substitute
from horus_runtime.context import HorusContext
from horus_runtime.core.runtime.events import RuntimeEvent
from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class PythonScriptRuntime(CommandRuntime):
    """
    Ship a local Python script to the task's target and run it there.

    The script is read from the orchestrator and written into the task's
    working directory on the target (local or remote) via the target channel,
    so the user never has to construct a remote path. Swapping the task's
    target is enough to move the same workflow between machines.

    Subclasses :class:`CommandRuntime` so it runs through ``ShellExecutor``
    unchanged; the inherited ``command`` field is unused (the command is built
    here from ``script``/``args``).
    """

    kind: str = "python_script"
    kind_name: ClassVar[str] = "Python Script"
    kind_description: ClassVar[str] = _(
        "Run a local Python script file on the task's target."
    )

    script: Path
    """Local path to the ``.py`` file to run."""

    args: str = ""
    """Extra CLI args appended after the script; supports ``$input`` /
    ``${output}`` placeholders (resolved to their on-target paths)."""

    python: str = "python"
    """Interpreter to invoke on the target (override if not ``python``)."""

    command: str = ""

    def anchor_local_paths(self, base: Path) -> None:
        """Resolve ``script`` against ``base`` if it is a relative path."""
        if not self.script.is_absolute():
            self.script = (base / self.script).resolve()

    async def _setup_runtime(self, task: "BaseTask") -> str:
        remote_path = f"{task.working_dir}/{self.script.name}"

        # Placing the file also creates task.working_dir on the target, which
        # the executor uses as cwd and where the script's outputs land.
        await task.target.put_file(self.script, remote_path)

        args = substitute(self.args, task) if self.args else ""
        cmd = f"{self.python} {shlex.quote(remote_path)} {args}".rstrip()
        self.formatted_command = cmd

        HorusContext.get_context().bus.emit(
            RuntimeEvent(
                runtime_kind=self.kind,
                task_id=task.id,
                details={"formatted_command": cmd},
            )
        )
        return cmd
