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
            f"Another workflow with ID {existing_workflow.id} is already "
            "running in this context. Only one workflow can run at a time "
            "per context."
        )
