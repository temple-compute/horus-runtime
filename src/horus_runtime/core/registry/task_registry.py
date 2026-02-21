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
Task registry
"""

from typing import TYPE_CHECKING, TypeAlias

from horus_runtime.core.registry.auto_registry import init_registry
from horus_runtime.core.task.base import BaseTask

# Check ArtifactRegistry for an explanation of this trick
if TYPE_CHECKING:
    TaskUnion: TypeAlias = BaseTask
else:
    TaskUnion = init_registry(BaseTask, "horus.tasks")
