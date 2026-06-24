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
Explicit connection between a producer's output artifact and a consumer's
input artifact. Edges are the source of truth for the workflow DAG: a
``source`` task must complete before its ``target`` task.
"""

from pydantic import BaseModel


class WorkflowEdge(BaseModel):
    """
    A directed connection feeding one task's input from another task's output
    (or from a root artifact).
    """

    source: str
    """Producer task id, or ``artifact-<rootId>`` for a root source."""

    source_output: str
    """Output artifact id on the source (or the root artifact's id)."""

    target: str
    """Consumer task id."""

    target_input: str
    """Input artifact id on the consumer task."""
