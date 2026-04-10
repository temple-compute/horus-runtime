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
WorkflowStatus represents the current state of a workflow's execution.
"""

from enum import Enum


class WorkflowStatus(Enum):
    """
    Enumeration of possible workflow statuses.
    """

    IDLE = "idle"
    """
    The workflow has been created but not yet started.
    """

    RUNNING = "running"
    """
    The workflow is currently executing tasks.
    """

    COMPLETED = "completed"
    """
    All tasks finished successfully or were skipped.
    """

    FAILED = "failed"
    """
    A task failed and the workflow halted. The failed task's status identifies
    the point of failure.
    """

    CANCELED = "canceled"
    """
    The workflow was explicitly cancelled mid-run. Tasks that had not yet been
    dispatched remain IDLE.
    """

    PARTIAL = "partial"
    """
    The workflow halted cleanly before completing all tasks (e.g. graceful
    stop requested). Distinguished from CANCELED to support resumption: a
    PARTIAL workflow can be resumed from the first incomplete task, whereas
    CANCELED implies deliberate termination.
    """
