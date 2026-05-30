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
    same output artifact ID.
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
