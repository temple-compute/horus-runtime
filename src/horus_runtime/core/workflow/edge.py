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

Omitting both artifact ids makes the edge purely ordering: it needs no
artifact on either end, so it can order tasks that declare none.
"""

from typing import Self

from pydantic import BaseModel, model_validator

from horus_runtime.core.workflow.exceptions import IncompleteEdgeError


class WorkflowEdge(BaseModel):
    """
    A directed connection feeding one task's input from another task's output
    (or from a root artifact).
    """

    source: str
    """Producer task id, or ``artifact-<rootId>`` for a root source."""

    source_output: str | None = None
    """
    Output artifact id on the source (or the root artifact's id). ``None``
    only on an artifact-less ordering edge (see ``transfer``).
    """

    target: str
    """Consumer task id."""

    target_input: str | None = None
    """
    Input artifact id on the consumer task. ``None`` only on an artifact-less
    ordering edge (see ``transfer``).
    """

    transfer: bool = True
    """
    Whether this edge also carries data: when ``True`` (the default) the
    source's output is transferred to the target input, as every existing
    edge has always done. When ``False`` the edge is ordering-only: it still
    makes ``target`` depend on ``source`` in the DAG, but contributes no
    transfer source for ``target_input`` and is exempt from the "at most one
    edge per (target, target_input)" rule, so several ordering-only edges (or
    one ordering-only edge alongside a single ``transfer=True`` edge) may all
    feed the same input. This is what lets many producers order-gate one
    consumer whose actual data input is, say, a folder populated out of band.

    Forced to ``False`` when the edge names no artifacts at all: there is
    then nothing to carry. Such an edge orders two tasks that need declare no
    inputs or outputs, which ``transfer=False`` alone cannot express (it still
    requires both ids to name declared artifacts).
    """

    @model_validator(mode="after")
    def check_endpoints_agree(self) -> Self:
        """
        Both artifact ids or neither, and an edge naming none cannot transfer.

        A half-specified edge is a typo: letting it through would silently
        downgrade a data edge to an ordering one. Forcing ``transfer=False``
        when both ids are absent keeps ``transfer`` the single question every
        consumer already asks ("does this edge carry data?"), so nothing
        downstream needs to re-derive it from the ids.
        """
        if (self.source_output is None) != (self.target_input is None):
            raise IncompleteEdgeError(self.source, self.target)
        if self.target_input is None:
            self.transfer = False
        return self
