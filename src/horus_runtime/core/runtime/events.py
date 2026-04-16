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
Runtime events. These events are emitted by the runtime during task execution.
"""

from horus_runtime.event.base import BaseEvent


class RuntimeEvent(BaseEvent):
    """
    Event emitted by the runtime during task execution.
    """

    event_type: str = "runtime_event"
    runtime_kind: str
    task_id: str
    details: dict[str, str] | None = None
