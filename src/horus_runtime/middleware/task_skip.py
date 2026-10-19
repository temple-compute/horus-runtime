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
Task skip middleware system for the horus-runtime.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from horus_runtime.middleware.auto_middleware import AutoMiddleware

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask, InputDigests
    from horus_runtime.core.workflow.base import BaseWorkflow


@dataclass
class TaskSkipMiddlewareContext:
    """
    Context passed to TaskSkipMiddleware. ``input_digests`` gives the
    task's input digests at their source, computed on the first await and
    shared with the bound-task middleware and the check of the first
    attempt. A pinned input is digested on ``task.target``, which is not
    bound yet here, so don't await it for a task with one.

    Set ``complete`` in ``before`` to skip the task as complete (when
    ``task.skip_if_complete``), before any slot or target is acquired.
    Only do so when its outputs can be read without it, e.g. relocated
    with ``workflow.relocate_output``: the task never reaches its machine,
    so the lease manager records nothing there for it.
    """

    task: "BaseTask"
    workflow: "BaseWorkflow"
    input_digests: "InputDigests"
    complete: bool = False


class TaskSkipMiddleware(
    AutoMiddleware[TaskSkipMiddlewareContext], entry_point="task_skip"
):
    """
    Runs once per placed task, before its lease slot, placement and pool
    slot, so a task judged complete from metadata never waits behind
    running tasks. Not called again on a node-loss retry.

    An exception fails the task, as one from a bound-task middleware does.
    """

    registry: list[type["TaskSkipMiddleware"]]
