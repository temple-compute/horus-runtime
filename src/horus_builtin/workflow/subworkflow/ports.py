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
Port derivation for the subworkflow construct.

Kept separate from :mod:`horus_builtin.workflow.subworkflow.expander` so it
can be imported (directly, or via the package's ``__init__``) without
pulling in a real ``BaseWorkflow`` import: this module only ever needs
``BaseWorkflow`` as a type hint, never to construct or validate one.
"""

from collections import Counter
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from horus_runtime.core.workflow.base import BaseWorkflow


class SubworkflowPort(BaseModel):
    """
    One derived port of a subworkflow body.

    A port is the parent-visible name of something already present in the
    child: a root artifact (in-port) or an unconsumed task output
    (out-port).
    """

    name: str
    """Parent-visible port name (post ``port_overrides``)."""

    artifact: str
    """Artifact id inside the body (root id, or the producer's output id)."""

    task: str | None = None
    """Inner producer task id; ``None`` for an in-port (a root artifact)."""


def derive_ports(
    body: "BaseWorkflow",
    port_overrides: dict[str, str] | None = None,
) -> tuple[list[SubworkflowPort], list[SubworkflowPort]]:
    """
    Derive a subworkflow body's input and output ports.

    This is the single definition of a subworkflow's interface, shared by
    the load-time validator and the runtime expansion. Nothing is declared
    twice: the protocol lives in the child workflow itself.

    Args:
        body: A complete ``BaseWorkflow``.
        port_overrides: Optional ``derived_name -> new_name`` renames,
            applied last.

    Returns:
        ``(in_ports, out_ports)``. In-ports are the body's root artifacts
        (``body.artifacts``), which inner edges reference as
        ``artifact-<rootId>``. Out-ports are the task outputs no inner edge
        consumes, named by artifact id and qualified to ``taskid.artifactid``
        when two of them share an id.
    """
    overrides = port_overrides or {}

    def rename(name: str) -> str:
        return overrides.get(name, name)

    in_ports = [
        SubworkflowPort(name=rename(art.id), artifact=art.id)
        for art in body.artifacts
    ]

    consumed = {(edge.source, edge.source_output) for edge in body.edges}
    leaves = [
        (task.id, out.id)
        for task in body.tasks
        for out in task.outputs
        if (task.id, out.id) not in consumed
    ]
    seen = Counter(artifact for _task, artifact in leaves)
    out_ports = [
        SubworkflowPort(
            name=rename(
                artifact if seen[artifact] == 1 else f"{task}.{artifact}"
            ),
            artifact=artifact,
            task=task,
        )
        for task, artifact in leaves
    ]

    return in_ports, out_ports
