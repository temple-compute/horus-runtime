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
Base Workflow. The workflow orchestrates an ordered set of tasks, using
artifact existence and integrity to determine which tasks need to run.

Each task declares its output artifacts. A task is skipped if all of its
outputs already exist, because the workflow treats output artifact presence
as proof of prior successful completion. Any task with no declared outputs
always runs unconditionally.

The workflow executes tasks in the order they are defined. It does not
currently perform dependency resolution; ordering is the author's
responsibility when writing the workflow YAML file.
"""

from abc import abstractmethod
from asyncio import CancelledError
from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, model_validator

from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.workflow.status import WorkflowStatus
from horus_runtime.registry.auto_registry import AutoRegistry


class BaseWorkflow(AutoRegistry, entry_point="workflow"):
    """
    Orchestrates an ordered collection of tasks.
    """

    registry_key: ClassVar[str] = "kind"

    kind: str
    """
    The 'kind' field is used to identify the specific type of workflow.
    """

    name: str
    """
    Human-readable name for this workflow.
    """

    tasks: dict[str, BaseTask] = Field(
        default_factory=dict,
    )
    """
    Ordered mapping of task names to task instances.
    """

    status: WorkflowStatus = WorkflowStatus.IDLE
    """
    Current execution state of the workflow. Updated automatically by
    ``run()``; do not set manually.
    """

    @model_validator(mode="after")
    def inject_task_ids(self) -> Self:
        """
        After workflow initialization, inject task IDs to each task.
        """
        for tid, task in self.tasks.items():
            task.task_id = tid

        return self

    @classmethod
    @abstractmethod
    def from_yaml(cls, path: str | Path) -> Self:
        """
        Load a workflow from a YAML file.

        Args:
            path: Path to the YAML file.

        Returns:
            A fully constructed :class:`BaseWorkflow` instance.
        """

    async def run(self) -> None:
        """
        Execute the workflow, managing status transitions automatically.

        Subclasses must implement ``_run()`` instead of overriding this method.
        Status is driven entirely here:

        - ``RUNNING``   — set immediately on entry
        - ``COMPLETED`` — set on clean exit
        - ``CANCELED``  — set when ``CancelledError`` is raised
        - ``FAILED``    — set on any other exception (re-raised after)
        """
        self.status = WorkflowStatus.RUNNING
        try:
            await self._run()
        except CancelledError:
            self.status = WorkflowStatus.CANCELED
            raise
        except Exception:
            self.status = WorkflowStatus.FAILED
            raise
        else:
            self.status = WorkflowStatus.COMPLETED

    @abstractmethod
    async def _run(self) -> None:
        """
        Workflow-specific execution logic. Implement this in subclasses.
        Do not set ``self.status`` here; ``run()`` manages it.
        """

    @abstractmethod
    def reset(self) -> None:
        """
        Reset the workflow by resetting all tasks.
        """
