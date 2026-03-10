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
import inspect
import uuid
from typing import Any, ClassVar, Literal

from pydantic import ConfigDict, Field

from horus_runtime.registry.auto_registry import AutoRegistry


def _get_current_frame_info() -> str:
    """
    Utility function to get the caller's frame information for event source.
    Walks the call stack to find the first frame outside of pydantic internals
    to provide a more meaningful source for events.
    """
    frame = inspect.currentframe()

    if frame:
        while frame := frame.f_back:
            module = frame.f_globals.get("__name__", "")
            if not module.startswith("pydantic"):
                code = frame.f_code
                return getattr(code, "co_qualname", code.co_name)

    return "unknown"


class BaseEvent(AutoRegistry, entry_point="event"):
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

    source: str = Field(default_factory=_get_current_frame_info)
    """
    Source of the event. Automaticaly inferred from the caller's frame
    information if not provided.
    """

    message: str | None = None
    """
    Optional message or payload for the event.
    """

    model_config = ConfigDict(frozen=True)
