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
Workflow exceptions.
"""

from typing import TYPE_CHECKING

from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.workflow.base import BaseWorkflow


class WorkflowError(Exception):
    """Base exception for workflow-related errors."""


class OneWorkflowAtATimeError(WorkflowError):
    """
    Raised when trying to run a workflow while another is already
    running in the same context.
    """

    def __init__(self, existing_workflow: "BaseWorkflow"):
        super().__init__(
            _(
                "Another workflow with ID %(id)s is already "
                "running in this context. "
                "Only one workflow can run at a time per context."
            )
            % {"id": existing_workflow.id}
        )


class TaskIdsAreNotUniqueError(WorkflowError):
    """
    Raised when two or more tasks in the workflow share the same ID.
    """

    def __init__(self, duplicate_id: str):
        super().__init__(
            _(
                "Multiple tasks share the same ID '%(id)s'. "
                "Task IDs must be unique within a workflow."
            )
            % {"id": duplicate_id}
        )


class ArtifactIdsAreNotUniqueError(WorkflowError):
    """
    Raised when two or more tasks in the workflow declare the
    same artifact ID inside inputs or outputs.
    """

    def __init__(self, duplicate_id: str):
        super().__init__(
            _(
                "Multiple tasks declare the same output artifact ID '%(id)s'. "
                "Output artifact IDs must be unique across all "
                "tasks in a workflow."
            )
            % {"id": duplicate_id}
        )


class UnknownEdgeEndpointError(WorkflowError):
    """
    Raised when a workflow edge references a task, output, input, or root
    artifact that does not exist. Edges are the sole source of truth for the
    DAG, so an unresolved endpoint would silently drop a dependency or
    misroute an artifact transfer.
    """

    def __init__(self, endpoint: str, value: str):
        super().__init__(
            _("Workflow edge references unknown %(endpoint)s '%(value)s'.")
            % {"endpoint": endpoint, "value": value}
        )


class DuplicateEdgeTargetError(WorkflowError):
    """
    Raised when two edges feed the same consumer input. A single input can be
    sourced by at most one edge; otherwise transfer resolution would silently
    keep only the last edge.
    """

    def __init__(self, target: str, target_input: str):
        super().__init__(
            _(
                "Multiple edges feed input '%(input)s' of task '%(target)s'. "
                "Each consumer input may be fed by at most one edge."
            )
            % {"input": target_input, "target": target}
        )


class WorkflowExecutionError(WorkflowError):
    """
    Raised by the scheduler when a run finishes under the ``"continue"``
    failure policy (see :attr:`BaseWorkflow.failure_policy`) with one or more
    failed tasks.

    Under ``"fail_fast"`` the triggering task's own exception propagates
    directly, so this error never applies there. Under ``"continue"`` the
    scheduler lets unrelated branches run to completion before reporting the
    failure, so this error is raised afterwards, naming every task that
    failed, to still transition the workflow to ``FAILED``.
    """

    def __init__(self, failed_task_ids: list[str]):
        self.failed_task_ids = failed_task_ids
        super().__init__(
            _("Workflow finished with failed task(s): %(tasks)s.")
            % {"tasks": ", ".join(failed_task_ids)}
        )
