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
Default Horus task implementation.
"""

from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.target.local import LocalTarget
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.exceptions import ArtifactDoesNotExistError
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.i18n import tr as _
from horus_runtime.utils.timing import timed


class HorusTask(BaseTask):
    """
    The HorusTask represents a basic task in the Horus runtime.
    """

    kind: str = "horus_task"

    target: BaseTarget = LocalTarget()
    """
    The default target for a HorusTask is LocalTarget (in-process).
    """

    async def _run(self) -> None:
        """
        For a HorusTask, nothing needs to be done here, as the command is
        already specified in the runtime and will be executed by the executor.
        """
        ctx = HorusContext.get_context()

        ctx.bus.emit(
            HorusTaskEvent(
                task_id=self.task_id,
                task_name=self.name,
                message=_("Task %(task_name)s started.")
                % {"task_name": self.name},
            )
        )

        self.runs += 1

        # Gather inputs
        for (
            input_name,
            artifact,
        ) in self.inputs.items():
            if not artifact.exists():
                raise ArtifactDoesNotExistError(
                    _("Input artifact %(input_name)s does not exist")
                    % {"input_name": input_name}
                )

        # Execute the command using the executor
        with timed() as get_elapsed:
            return_code = await self.executor.execute(self)

        # Get the elapsed time and emit the completion event
        elapsed = get_elapsed()

        ctx.bus.emit(
            HorusTaskEvent(
                task_id=self.task_id,
                task_name=self.name,
                data={
                    "return_code": return_code,
                    "elapsed_time": elapsed,
                },
                message=_(
                    "Task %(task_name)s completed in %(elapsed).2f seconds."
                )
                % {"task_name": self.name, "elapsed": elapsed},
            )
        )

        if return_code != 0:
            raise TaskExecutionError(
                _("Task execution failed with return code %(return_code)s")
                % {"return_code": return_code}
            )

    def is_complete(self) -> bool:
        """
        A HorusTask is considered complete if all of its output artifacts
        exist.
        """
        # If no outputs are declared, we consider the task incomplete and
        # always run it
        if not self.outputs:
            return False

        for artifact in self.outputs.values():
            if not artifact.exists():
                return False

        return True

    def _reset(self) -> None:
        """
        Reset the task by deleting all output artifacts. This allows the task
        to be re-run from scratch.
        """
        ctx = HorusContext.get_context()

        ctx.bus.emit(
            HorusTaskEvent(
                message=_("Resetting task %(task_name)s.")
                % {"task_name": self.name},
                task_id=self.task_id,
                task_name=self.name,
            )
        )

        for artifact in self.outputs.values():
            artifact.delete()

        self.runs = 0
