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

from horus_runtime.context import HorusContext
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.runtime.events import RuntimeEvent
from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.artifact.base import BaseArtifact
    from horus_runtime.core.target.base import BaseTarget
    from horus_runtime.core.task.base import BaseTask


# Wraps an artifact so that "{script}" in a command formats to the artifact's
# path *on the task's target* (target.path_on_target), while "{script.path}",
# "{script.id}", etc. still forward to the artifact. This is what lets a
# command be written once and run unchanged on a local or remote target.
class _ArtifactRef:
    def __init__(self, artifact: "BaseArtifact", target: "BaseTarget"):
        self._a = artifact
        self._t = target

    def __getattr__(self, name: str) -> object:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._a, name)

    def __format__(self, spec: str) -> str:
        return format(self._t.path_on_target(self._a), spec)


# Create a namespace object to allow for attribute-style access to task
# variables and inputs in the command formatting. This allows users to
# write commands like "echo {task.input1.path}" in the workflow yaml
class _TaskNamespace:
    def __init__(self, task: "BaseTask"):
        for name, value in vars(task).items():
            setattr(self, name, value)
        for artifact in (*task.inputs, *task.outputs):
            setattr(self, artifact.id, _ArtifactRef(artifact, task.target))


def format_command(template: str, task: "BaseTask") -> str:
    """
    Render *template* against *task*: artifacts are exposed by id (``{script}``
    resolves to the artifact's on-target path) and via the ``task`` namespace.
    """
    artifacts = (*task.inputs, *task.outputs)
    if any(a.id == "task" for a in artifacts):
        raise ValueError(
            _(
                "Artifact id 'task' is reserved for command templates. "
                "Please rename this artifact."
            )
        )
    refs = {a.id: _ArtifactRef(a, task.target) for a in artifacts}

    return template.format(task=_TaskNamespace(task), **refs)


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
    The command to execute.
    """

    formatted_command: str = ""
    """
    The formatted command after processing any placeholders.
    """

    async def _setup_runtime(self, task: "BaseTask") -> str:
        """
        For the CommandRuntime, setting up the runtime simply involves
        returning the command as is, since there are no placeholders to
        replace.
        """
        fmt = format_command(self.command, task)

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
