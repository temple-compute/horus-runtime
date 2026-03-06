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
Definitions of the workflow registry.
"""

from typing import TYPE_CHECKING, TypeAlias

from horus_runtime.core.registry.auto_registry import init_registry
from horus_runtime.core.workflow.base import BaseWorkflow

# We define a type alias for the registry union type to make it easier to use
# in type annotations throughout the codebase. We need to "trick" the type
# checker here because the registry union type is dynamically generated at
# runtime and can't be easily expressed as a static type annotation, so we
# assign BaseWorkflow during development
if TYPE_CHECKING:
    WorkflowUnion: TypeAlias = BaseWorkflow
else:
    WorkflowUnion = init_registry(BaseWorkflow, "horus.workflows")
