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
ArtifactEvent. Emitted when an artifact is created, updated, or deleted.
"""

from enum import Enum
from typing import ClassVar

from horus_runtime.event.base import BaseEvent


class ArtifactEventsEnum(Enum):
    """
    Standard artifact events.
    """

    DELETE = "deleted"
    PACKAGE = "packaged"
    UNPACKAGE = "unpackaged"
    READ = "read"
    WRITE = "written"


class ArtifactEvent(BaseEvent):
    """
    Event emitted when an artifact is created, updated, or deleted.
    """

    add_to_registry: ClassVar[bool] = True
    event_type: str = "artifact_event"
    event_name: ArtifactEventsEnum
    artifact_id: str
