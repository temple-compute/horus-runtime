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
HorusWorkflow implementation for Horus built-in workflows.
"""

from typing import ClassVar

from horus_builtin.target.local import LocalTarget
from horus_builtin.workflow.dag import execution_plan
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger


class HorusWorkflow(BaseWorkflow):
    """
    Basic implementation of the Workflow class for Horus built-in workflows.

    The workflow determines whether each task needs to run by inspecting the
    existence of its declared output artifacts. Tasks that have already
    produced all their outputs are skipped, which provides basic incremental
    execution: re-running a workflow only re-executes tasks whose outputs are
    missing.
    """

    kind: str = "horus_workflow"
    kind_name: ClassVar[str] = "Horus Workflow"
    kind_description: ClassVar[str] = _(
        "The default workflow implementation for Horus built-in workflows."
    )

    orchestrator_target: BaseTarget = LocalTarget()
    """
    The orchestrator runs locally; root input artifacts (those not produced by
    any upstream task) are sourced from the local filesystem.
    """

    async def _run(self, trigger_id: str) -> None:
        """
        A task is skipped when all of
        its output artifacts exist (see :meth:`is_complete`).
        """
        tasks = {task.id: task for task in self.tasks}

        # No edges means no dependencies: every task runs independently and the
        # plan is limited to the trigger's own (singleton) scope. Flag it so a
        # workflow that forgot to wire its edges is diagnosable.
        if len(self.tasks) > 1 and not self.edges:
            horus_logger.log.debug(
                _(
                    "Workflow %(name)s has multiple tasks but no edges; "
                    "tasks run independently with no ordering."
                )
                % {"name": self.name}
            )

        plan = execution_plan(
            self.tasks, trigger_id=trigger_id, edges=self.edges
        )

        # The edge source map depends only on workflow structure, so build it
        # once and reuse it for every task instead of rebuilding per task.
        source_map = self._build_source_map()

        for task_id in plan:
            task = tasks[task_id]

            # Associate the task with its target before any transfer so
            # resource-aware targets (which may provision lazily at transfer
            # time, before dispatch) can read task.resources.
            task.target.bind(task)

            # Transfer input artifacts to the task's target as needed.
            await self.transfer_artifacts(task, source_map)

            # Execute the task on its target
            await task.target.dispatch(task)

            # Wait for the task to complete and check for failure
            await task.target.wait()

    def _reset(self) -> None:
        """
        Reset the workflow by deleting all output artifacts of all tasks in the
        workflow. This allows the workflow to be re-run from scratch.
        """
        for task in self.tasks:
            task.reset()
