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
YAML sugar lowering for the subworkflow construct.
"""

from typing import Any


def lower_subworkflow_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """
    Lower one raw YAML task-dict carrying a ``sub:`` block into a
    ``kind: subworkflow`` task-dict.

    Because ports are derived from the body, the sugar is just the child
    workflow written inline. There is no port or binding block to author.

    Args:
        entry: The raw task dict as parsed from YAML, carrying ``id`` and a
            ``sub`` key holding a complete child workflow document.

    Returns:
        A ``kind: subworkflow`` task dict ready for
        ``BaseTask.model_validate``.
    """
    task_id = entry["id"]
    data: dict[str, Any] = {
        "kind": "subworkflow",
        "id": task_id,
        "name": entry.get("name") or task_id,
        "description": entry.get("description", ""),
        "body": entry["sub"],
    }
    if entry.get("port_overrides"):
        data["port_overrides"] = entry["port_overrides"]
    if entry.get("max_depth") is not None:
        data["max_depth"] = entry["max_depth"]
    if entry.get("target") is not None:
        data["target"] = entry["target"]
    return data
