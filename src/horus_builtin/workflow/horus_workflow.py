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
from horus_builtin.workflow.scheduler import run_schedule
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.i18n import tr as _


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
        Dispatch tasks via the concurrent ready-set scheduler.

        A task is skipped when all of its output artifacts exist (see
        :meth:`is_complete`). Every task whose dependencies are satisfied
        runs as soon as it is ready, concurrently with any other ready task
        (bounded by :attr:`max_concurrency` when set); see
        :func:`horus_builtin.workflow.scheduler.run_schedule` for the
        scheduling loop itself.
        """
        await run_schedule(self, trigger_id)

    async def _reset(self) -> None:
        """
        Reset the workflow by deleting all output artifacts of all tasks in the
        workflow. This allows the workflow to be re-run from scratch.
        """
        for task in self.tasks:
            await task.reset()
