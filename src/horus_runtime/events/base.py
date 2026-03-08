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
Base event class for horus-runtime.
"""

import datetime
import uuid
from abc import ABC
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from horus_runtime.registry.auto_registry import AutoRegistry


class BaseEvent(BaseModel, ABC, AutoRegistry):
    """
    Base event class. All Horus events should inherit from this class.
    """

    add_to_registry: ClassVar[bool] = False
    """
    Base event class should not be added to the registry.
    """

    event_id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
    )
    """
    Unique identifier for the event. Automatically generated if not provided.
    """

    registry_key: ClassVar[Literal["event_type"]] = "event_type"
    """
    The key used to register the event in the registry.
    """

    event_type: Any = ...
    """
    Must be defined by subclasses.
    """

    timestamp: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    """
    Timestamp of when the event was created.
    """

    source: str
    """
    Optional source of the event.
    """

    class Config:
        """
        Pydantic configuration for BaseEvent.
        """

        frozen = True
