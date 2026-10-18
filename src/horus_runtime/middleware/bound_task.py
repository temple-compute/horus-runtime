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
Bound task middleware system for the horus-runtime.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from horus_runtime.middleware.auto_middleware import AutoMiddleware

if TYPE_CHECKING:
    from horus_runtime.core.target.base import BaseTarget
    from horus_runtime.core.task.base import BaseTask


@dataclass
class BoundTaskMiddlewareContext:
    """
    Context passed to BoundTaskMiddleware. ``target`` is the acquired
    target, already bound to ``task`` (``task.target is target``).
    """

    task: "BaseTask"
    target: "BaseTarget"


class BoundTaskMiddleware(
    AutoMiddleware[BoundTaskMiddlewareContext], entry_point="bound_task"
):
    """
    Wraps one attempt of a task on the target it is bound to: the
    completion check before transfer, then transfer, dispatch and wait.
    Runs for every task the scheduler places, skipped or not. A node lost
    during the attempt re-binds the task and wraps the retry again, so
    ``before`` sees the new node.
    """

    registry: list[type["BoundTaskMiddleware"]]
