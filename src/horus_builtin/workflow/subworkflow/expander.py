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
``SubworkflowExpander``: inlines a complete child workflow into the parent's
live DAG. See :mod:`horus_builtin.workflow.subworkflow` for the full design.
"""

from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import Field, field_serializer, model_validator
from pydantic_core.core_schema import SerializerFunctionWrapHandler

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.subworkflow.errors import SubworkflowError
from horus_builtin.workflow.subworkflow.ports import (
    SubworkflowPort,
    derive_ports,
)
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.store import ArtifactStore
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

_ROOT_PREFIX = "artifact-"
_ID_SEPARATOR = "/"
_PORT_SUFFIX = ".port"


def _restore_declared_paths(
    entries: list[dict[str, Any]], artifacts: list[BaseArtifact]
) -> None:
    """
    Undo the eager CWD resolution ``BaseArtifact`` applies at construction.

    ``BaseArtifact.path`` is resolved to an absolute, CWD-anchored path the
    moment the artifact is built, while the original (possibly relative)
    value is kept separately on ``declared_path`` (excluded from
    ``model_dump``). Dumping a body as-is would therefore bake in the
    *parent's* process CWD instead of leaving relative paths for the run
    directory to anchor. Restoring each artifact's ``declared_path`` onto
    the dumped ``path`` makes the document say what its author wrote.
    """
    for entry, artifact in zip(entries, artifacts, strict=False):
        if artifact.declared_path is not None:
            entry["path"] = str(artifact.declared_path)


class SubworkflowExpander(HorusTask):
    """
    Inlines a complete child workflow into the parent's live DAG.

    See the package docstring for port derivation and boundary rewiring.
    """

    kind: str = "subworkflow"
    kind_name: ClassVar[str] = "Subworkflow"
    kind_description: ClassVar[str] = _(
        "Inlines a complete child workflow's tasks and edges into the "
        "parent workflow at run time."
    )

    runtime: BaseRuntime = Field(
        default_factory=lambda: CommandRuntime(command="true")
    )
    """
    Inert placeholder: :meth:`_run` is fully overridden and never delegates
    to ``self.executor``/``self.runtime``, so these exist only to satisfy
    ``BaseTask``'s required fields.
    """

    executor: BaseExecutor = Field(default_factory=ShellExecutor)
    target: BaseTarget = Field(default_factory=LocalTarget)

    body: BaseWorkflow
    """
    A complete, valid child ``BaseWorkflow``. Any existing workflow can be
    dropped in unchanged: its ports are derived from it (see
    :func:`~horus_builtin.workflow.subworkflow.ports.derive_ports`), so
    nothing about it has to be adapted first.
    """

    port_overrides: dict[str, str] = Field(default_factory=dict)
    """Optional ``derived_name -> new_name`` renames for derived ports."""

    @field_serializer("body", mode="wrap")
    def _dump_body(
        self, body: BaseWorkflow, handler: SerializerFunctionWrapHandler
    ) -> Any:
        """
        Dump ``body`` with declared (pre-resolution) artifact paths, not the
        eagerly CWD-resolved ones ``BaseArtifact`` carries at runtime.

        This matters whenever a ``SubworkflowExpander`` is itself dumped and
        reloaded elsewhere (``to_yaml``/``from_yaml``, or a ``MapExpander``
        capturing this as its per-clone template): without it, a relative
        artifact path would be baked in absolute and never re-anchored to
        the eventual run directory. See :func:`_restore_declared_paths`.
        """
        document = handler(body)
        _restore_declared_paths(
            document.get("artifacts") or [], body.artifacts
        )
        for entry, task in zip(
            document.get("tasks") or [], body.tasks, strict=False
        ):
            _restore_declared_paths(
                entry.get("inputs") or [], list(task.inputs)
            )
            _restore_declared_paths(
                entry.get("outputs") or [], list(task.outputs)
            )
        return document

    max_depth: int = 10
    """
    Maximum nesting depth, counted from the ``/``-separated inner-id prefix.
    Guards against a body that (transitively) embeds itself.
    """

    @model_validator(mode="after")
    def _ensure_ports(self) -> Self:
        """
        Append one placeholder artifact per derived port, idempotently (so
        a dumped-and-reloaded expander does not duplicate them).

        ``self.body`` is already a validated ``BaseWorkflow`` by this point
        (pydantic validates it as a nested field, reusing every existing
        workflow validation: unique ids, edges resolve, no cycles), so
        there is nothing left to check here but the ``/``-in-id rule that
        is specific to inlining.
        """
        for task in self.body.tasks:
            if _ID_SEPARATOR in task.id:
                raise SubworkflowError(
                    _(
                        "Subworkflow '%(id)s' body task id '%(inner)s' may "
                        "not contain '/': it is reserved for the inlined "
                        "id prefix."
                    )
                    % {"id": self.id, "inner": task.id}
                )

        in_ports, out_ports = derive_ports(self.body, self.port_overrides)
        for port in in_ports:
            if not any(a.id == port.name for a in self.inputs):
                self.inputs.append(self._placeholder(port.name))
        for port in out_ports:
            if not any(a.id == port.name for a in self.outputs):
                self.outputs.append(self._placeholder(port.name))
        return self

    def _placeholder(self, port_name: str) -> FileArtifact:
        """Build the never-written placeholder artifact for *port_name*."""
        slug = self.id.replace(_ID_SEPARATOR, "_")
        return FileArtifact(
            id=port_name, path=Path(f"{slug}.{port_name}{_PORT_SUFFIX}")
        )

    async def is_complete(self) -> bool:
        """
        Always incomplete: the expander must re-run on every trigger to
        re-derive its deterministic expansion. Each inlined task then
        decides independently, through ordinary ``skip_if_complete``
        behaviour, whether it still has work to do.
        """
        return False

    async def _reset(self) -> None:
        """
        Delete the (never-written) port placeholders, mirroring
        :meth:`~horus_builtin.task.horus_task.HorusTask._reset`. The inlined
        tasks and edges are re-derived from scratch on every :meth:`_run`
        and reset independently as ordinary tasks.
        """
        store = ArtifactStore(self.target)
        for artifact in self.outputs:
            await store.delete(artifact)
        self.runs = 0

    async def _run(self) -> None:
        """
        Inline the body: build the prefixed inner tasks, rewrite the inner
        edges, rewire the parent boundary edges, and commit everything in
        one atomic ``wf.expand(...)``. See the package docstring.
        """
        wf = self.workflow
        if wf is None:
            raise SubworkflowError(
                _("Subworkflow '%(id)s' must run inside a workflow.")
                % {"id": self.id}
            )
        if self.id.count(_ID_SEPARATOR) >= self.max_depth:
            raise SubworkflowError(
                _(
                    "Subworkflow '%(id)s' exceeds max_depth=%(depth)d; a "
                    "body that embeds itself would expand forever."
                )
                % {"id": self.id, "depth": self.max_depth}
            )

        self.runs += 1

        in_ports, out_ports = derive_ports(self.body, self.port_overrides)
        tasks = self._build_inner_tasks(wf.run_directory)
        by_id = {task.id: task for task in tasks}

        edges: list[WorkflowEdge] = []
        artifacts: list[BaseArtifact] = []
        feeds = {
            port.artifact: self._resolve_in_port(wf, port) for port in in_ports
        }

        for raw in self.body.edges:
            if raw.source.startswith(_ROOT_PREFIX):
                root = str(raw.source_output)
                self._wire_in_port(root, raw, feeds, by_id, edges, artifacts)
            else:
                edges.append(
                    WorkflowEdge(
                        source=self._prefixed(raw.source),
                        source_output=raw.source_output,
                        target=self._prefixed(raw.target),
                        target_input=raw.target_input,
                        transfer=raw.transfer,
                        condition=self._prefixed_condition(raw.condition),
                    )
                )

        edges.extend(self._out_port_edges(wf, out_ports))

        wf.expand(tasks=tasks, edges=edges, artifacts=artifacts)
        horus_logger.log.debug(
            _("Subworkflow '%(id)s' inlined %(n)d task(s).")
            % {"id": self.id, "n": len(tasks)}
        )

    def _prefixed(self, inner_id: str) -> str:
        """Parent-scope id of the inner element *inner_id*."""
        return f"{self.id}{_ID_SEPARATOR}{inner_id}"

    def _prefixed_condition(self, condition: Any) -> Any:
        """
        Carry an inner edge's condition through, re-pointing the task it
        reads its sentinel from at that task's inlined id.

        Everything else about the condition is preserved verbatim, so a
        branch authored inside a child workflow composes unchanged.
        """
        if condition is None or condition.source_task is None:
            return condition
        return condition.model_copy(
            update={"source_task": self._prefixed(condition.source_task)}
        )

    def _build_inner_tasks(self, run_root: Path) -> list[BaseTask]:
        """
        Reconstruct every body task as a fresh, prefixed parent-scope task.

        Relative artifact paths are re-rooted under a run-directory folder
        named after this expander, so two instances of the same body (two
        map clones, say) never write to the same place, and an inner input
        fed across the boundary lands inside the run rather than beside the
        workflow file.
        """
        tasks: list[BaseTask] = []
        for entry in self.body.tasks:
            if _ID_SEPARATOR in entry.id:
                raise SubworkflowError(
                    _(
                        "Subworkflow '%(id)s' body task id '%(inner)s' may "
                        "not contain '/'."
                    )
                    % {"id": self.id, "inner": entry.id}
                )
            task = entry.model_copy(deep=True)
            task.id = self._prefixed(entry.id)
            task.name = task.id
            # A forced re-run of the expander (e.g. CLI --no-skip-all)
            # must reach the tasks it inlines.
            if not self.skip_if_complete:
                task.skip_if_complete = False
            task.target = task.target.model_copy(deep=True)
            self._localize_paths(task, run_root)
            tasks.append(task)
        return tasks

    def _localize_paths(self, task: BaseTask, run_root: Path) -> None:
        """
        Re-root *task*'s still-relative artifact paths under this id.

        A nested expander is left alone: its artifacts are never-written
        port placeholders, and pinning them to an absolute path would be
        read back as "an enclosing construct materialized this port"
        (see :meth:`_resolve_in_port`).
        """
        if isinstance(task, SubworkflowExpander):
            return
        for artifact in (*task.inputs, *task.outputs):
            declared = artifact.declared_path
            if declared is None or declared.is_absolute():
                continue
            _pin(artifact, run_root / self.id / declared)

    def _resolve_in_port(
        self, wf: BaseWorkflow, port: SubworkflowPort
    ) -> tuple[str, Any]:
        """
        Decide how the child root artifact behind *port* is fed.

        Returns ``(mode, payload)``:

        - ``("pinned", path)`` when the port placeholder has been pinned to
          an absolute path by an enclosing construct (a ``MapExpander``
          materializes each clone's slice directly rather than through an
          edge), in which case inner consumers are pointed straight at it;
        - ``("edges", parent_edges)`` when the parent feeds the port
          through boundary edges, which are re-emitted onto the real inner
          consumers;
        - ``("root", None)`` when nothing feeds it, so the child keeps its
          own root artifact (re-registered under a prefixed id).
        """
        parent_edges = [
            edge
            for edge in wf.edges
            if edge.target == self.id and edge.target_input == port.name
        ]
        for edge in parent_edges:
            if edge.transfer:
                raise SubworkflowError(
                    _(
                        "Edge '%(source)s' -> subworkflow '%(id)s."
                        "%(port)s' must set transfer=False: the data is "
                        "carried by the edge re-emitted onto the inlined "
                        "consumer instead."
                    )
                    % {"source": edge.source, "id": self.id, "port": port.name}
                )

        if _is_pinned(self.inputs, port.name):
            placeholder = next(a for a in self.inputs if a.id == port.name)
            return "pinned", placeholder.declared_path
        if parent_edges:
            return "edges", parent_edges
        return "root", None

    def _wire_in_port(
        self,
        root: str,
        raw: WorkflowEdge,
        feeds: dict[str, tuple[str, Any]],
        by_id: dict[str, BaseTask],
        edges: list[WorkflowEdge],
        artifacts: list[BaseArtifact],
    ) -> None:
        """
        Rewire one inner ``artifact-<root>`` edge onto its parent source.

        Mutates *edges*/*artifacts* in place; see :meth:`_resolve_in_port`
        for the three feeding modes.
        """
        target = self._prefixed(raw.target)
        target_input = raw.target_input
        mode, payload = feeds.get(root, ("root", None))

        if mode == "pinned":
            consumer = by_id.get(target)
            artifact = (
                next(
                    (a for a in consumer.inputs if a.id == target_input),
                    None,
                )
                if consumer is not None
                else None
            )
            if artifact is not None:
                _pin(artifact, Path(payload))
            return

        if mode == "edges":
            for parent_edge in payload:
                edges.append(
                    WorkflowEdge(
                        source=parent_edge.source,
                        source_output=parent_edge.source_output,
                        target=target,
                        target_input=target_input,
                        transfer=True,
                        condition=parent_edge.condition,
                    )
                )
            return

        prefixed_root = self._prefixed(root)
        if not any(a.id == prefixed_root for a in artifacts):
            entry = next(
                (a for a in self.body.artifacts if a.id == root), None
            )
            if entry is None:
                raise SubworkflowError(
                    _(
                        "Subworkflow '%(id)s' body references unknown root "
                        "artifact '%(root)s'."
                    )
                    % {"id": self.id, "root": root}
                )
            artifacts.append(entry.model_copy(update={"id": prefixed_root}))
        edges.append(
            WorkflowEdge(
                source=f"{_ROOT_PREFIX}{prefixed_root}",
                source_output=prefixed_root,
                target=target,
                target_input=target_input,
                transfer=raw.transfer,
                condition=self._prefixed_condition(raw.condition),
            )
        )

    def _out_port_edges(
        self, wf: BaseWorkflow, out_ports: list[SubworkflowPort]
    ) -> list[WorkflowEdge]:
        """
        Re-emit every parent edge sourcing an out-port from the real inner
        producer, so the consumer receives the child's data rather than the
        never-written port placeholder.
        """
        edges: list[WorkflowEdge] = []
        for port in out_ports:
            if port.task is None or _is_pinned(self.outputs, port.name):
                # An enclosing construct (a MapExpander materializing this
                # clone's slot) has taken the port over and consumes the
                # placeholder directly. Known gap: fan-in of a mapped
                # subworkflow's results therefore sees the placeholder
                # rather than the inner producer's real output.
                continue
            for edge in wf.edges:
                if edge.source != self.id or edge.source_output != port.name:
                    continue
                if edge.transfer:
                    raise SubworkflowError(
                        _(
                            "Edge subworkflow '%(id)s.%(port)s' -> "
                            "'%(target)s' must set transfer=False: the data "
                            "is carried by the edge re-emitted from the "
                            "inlined producer instead."
                        )
                        % {
                            "id": self.id,
                            "port": port.name,
                            "target": edge.target,
                        }
                    )
                edges.append(
                    WorkflowEdge(
                        source=self._prefixed(port.task),
                        source_output=port.artifact,
                        target=edge.target,
                        target_input=edge.target_input,
                        transfer=True,
                        condition=edge.condition,
                    )
                )
        return edges


def _is_pinned(artifacts: list[BaseArtifact], port_name: str) -> bool:
    """Whether *port_name*'s placeholder was pinned to an absolute path."""
    artifact = next((a for a in artifacts if a.id == port_name), None)
    return (
        artifact is not None
        and artifact.declared_path is not None
        and artifact.declared_path.is_absolute()
    )


def _pin(artifact: BaseArtifact, path: Path) -> None:
    """
    Point *artifact* at *path* and mark it anchored, so the workflow's
    run-directory anchoring (applied by ``expand()``) leaves it untouched.
    """
    artifact.path = path
    artifact.declared_path = path


def subworkflow_task(
    wf: BaseWorkflow,
    *,
    id: str,
    body: "BaseWorkflow | dict[str, Any]",
    port_overrides: dict[str, str] | None = None,
    max_depth: int | None = None,
    name: str | None = None,
    target: BaseTarget | None = None,
) -> SubworkflowExpander:
    """
    Append a subworkflow task to *wf*.

    Mirrors the YAML ``sub:`` block (see
    :func:`~horus_builtin.workflow.subworkflow.lowering.lower_subworkflow_entry`),
    so the two authoring paths produce structurally equivalent tasks.

    Args:
        wf: The workflow to append to.
        id: Id of the subworkflow task, and the ``/``-separated prefix of
            every inlined inner id.
        body: The child workflow, either as a live ``BaseWorkflow`` (dumped
            here) or as an already-serialized document.
        port_overrides: Optional renames for derived ports.
        max_depth: Maximum nesting depth; defaults to the field default.
        name: Display name; defaults to *id*.
        target: Target the expander itself runs on; defaults to
            ``LocalTarget()``.

    Returns:
        The appended :class:`SubworkflowExpander`.
    """
    kwargs: dict[str, Any] = {
        "id": id,
        "name": name or id,
        "body": body,
        "port_overrides": port_overrides or {},
    }
    if max_depth is not None:
        kwargs["max_depth"] = max_depth
    if target is not None:
        kwargs["target"] = target

    expander = SubworkflowExpander(**kwargs)
    wf.tasks.append(expander)
    # Boundary edges never carry data themselves (see the package
    # docstring); force any that already exist, as the YAML lowering does.
    for edge in wf.edges:
        if id in (edge.source, edge.target):
            edge.transfer = False
    return expander
