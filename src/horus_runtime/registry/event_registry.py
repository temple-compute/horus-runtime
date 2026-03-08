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
Definition of the event registry for horus-runtime.
"""

from typing import TYPE_CHECKING, TypeAlias

from horus_runtime.events.base import BaseEvent
from horus_runtime.registry.auto_registry import init_registry

# Check ArtifactRegistry for an explanation of this trick
if TYPE_CHECKING:
    EventUnion: TypeAlias = BaseEvent
else:
    EventUnion = init_registry(BaseEvent, "horus.events")

# Expose the registry
EventRegistry = BaseEvent.registry
