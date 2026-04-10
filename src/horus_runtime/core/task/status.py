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
TaskStatus represents the current state of a task's execution.
"""

from enum import Enum


class TaskStatus(Enum):
    """
    Enumeration of possible task statuses.
    """

    IDLE = "idle"
    """
    The task has been created but not yet dispatched.
    """

    PENDING = "pending"
    """
    The task is dispatched, but has not started executing yet.
    """

    RUNNING = "running"
    """
    The task is currently executing.
    """

    COMPLETED = "completed"
    """
    The task has finished executing successfully.
    """

    FAILED = "failed"
    """
    The task has encountered an error during execution.
    """

    CANCELED = "canceled"
    """
    The task has been canceled before completion.
    """
