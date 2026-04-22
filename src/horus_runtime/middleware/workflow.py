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
Workflow middleware system for the horus-runtime.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from horus_runtime.middleware.auto_middleware import AutoMiddleware

if TYPE_CHECKING:
    from horus_runtime.core.workflow.base import BaseWorkflow


@dataclass
class WorkflowMiddlewareContext:
    """
    Context passed to WorkflowMiddleware.
    """

    workflow: "BaseWorkflow"


class WorkflowMiddleware(
    AutoMiddleware[WorkflowMiddlewareContext], entry_point="workflow"
):
    """
    Base class for workflow middleware.
    """

    registry: list[type["WorkflowMiddleware"]]
