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
Declarative subworkflow (composition) construct.

A ``subworkflow`` task carries a *complete* child workflow document in its
``body`` and, when it runs, inlines that child's tasks and edges into the
parent's live DAG. It is expressed either as a ``sub:`` block in YAML
(lowered by :func:`lower_subworkflow_entry`, hooked into
:class:`~horus_runtime.core.workflow.base.BaseWorkflow`'s ``model_validate``
pipeline) or via the :func:`subworkflow_task` Python builder
(``wf.subworkflow(...)``).

Inlining, not nesting
---------------------
The child is deliberately *not* executed by a nested ``BaseWorkflow.run``:
a nested run is rejected outright by ``OneWorkflowAtATimeError`` and would
give the child its own target pool and placement manager, so the parent's
``max_concurrency`` would no longer bound the whole run. Inlining instead
puts every inner task into the parent's ``wf.tasks`` with a live status,
which is also what lets a UI show per-child progress.

The interface is the child workflow itself
------------------------------------------
Ports are *derived* from the body (see :func:`derive_ports`), never
declared. The protocol a workflow offers is already written down inside it,
so there is no second place to keep in sync and no binding table to
validate:

- **Inputs are the child's root artifacts.** ``BaseWorkflow.artifacts`` is
  defined as "standalone root artifacts (no producer task)", referenced by
  inner edges through the ``artifact-<rootId>`` convention. That set *is* a
  workflow's input interface. The port name is the root artifact's id.
- **Outputs are task outputs no inner edge consumes** (the child's leaves).
  The port name is the artifact id, qualified to ``taskid.artifactid`` when
  two leaves share an id.

``port_overrides`` exists only to rename a derived port that reads badly or
collides; it is not a binding table.

Because parent edges must name ``(subworkflow_id, port)`` at *construction*
time, the expander declares one never-written placeholder artifact per
derived port, so the ordinary construction-time ``check_edges_resolve``
accepts them with no change to core validation. Those placeholders are
never written, so :meth:`SubworkflowExpander.is_complete` stays ``False``
and the expansion is re-derived deterministically on a resumed run.

Boundary rewiring
-----------------
:meth:`SubworkflowExpander._run` prefixes every inner id with
``f"{self.id}/{inner_id}"`` — ``/`` is unused by the map (``[i]``) and loop
(``#k``/``~k``) schemes, reads as a path, and nests
(``outer/inner/leaf``) — then:

- rewrites inner edges onto the prefixed ids, carrying ``condition``
  through verbatim so a conditional inside a subworkflow still composes;
- for an **in-port**, re-emits the parent edge that feeds
  ``(self.id, port)`` onto every inner task that consumed
  ``artifact-<port>``, and drops that child root artifact (the parent
  supplies it now);
- for an **out-port**, re-emits every parent edge sourcing
  ``(self.id, port)`` from the real inner producer instead.

Parent boundary edges must therefore be ``transfer=False``: the re-emitted
edge is the one that carries the data, and a second ``transfer=True`` edge
on the same ``(target, target_input)`` would make ``expand()`` raise
``DuplicateEdgeTargetError``. The YAML lowering and the Python builder both
force this; :meth:`_run` raises if it still finds one.

No ordering edges are emitted from the expander to its inner tasks:
``BaseWorkflow._gate_new_tasks_behind_creator`` (applied by ``expand()``)
already records an implicit dependency from each new task *with no incoming
task edge* onto the task that created it, so inner roots are gated behind
the expander and the rest of the inner graph follows transitively through
its own edges.

Everything is committed through a single atomic ``wf.expand(...)``.
"""

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel, Field, model_validator

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.store import ArtifactStore
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

if TYPE_CHECKING:
    from horus_runtime.core.workflow.base import BaseWorkflow

_ROOT_PREFIX = "artifact-"
_ID_SEPARATOR = "/"
_PORT_SUFFIX = ".port"


class SubworkflowError(WorkflowError):
    """Raised when a ``sub:`` block or a subworkflow body is malformed."""


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


def _restore_declared_paths(
    entries: list[dict[str, Any]], artifacts: list[BaseArtifact]
) -> None:
    """
    Undo the eager CWD resolution ``BaseArtifact`` applies at construction.

    A live workflow's artifact paths are already absolute by the time it is
    dumped, so a document made from one would pin every inner artifact
    beside the *parent's* process CWD instead of inside the run directory.
    Restoring each artifact's ``declared_path`` makes the document say what
    its author wrote, which is also exactly what a YAML body already says.
    """
    for entry, artifact in zip(entries, artifacts, strict=False):
        if artifact.declared_path is not None:
            entry["path"] = str(artifact.declared_path)


def _as_document(body: Any) -> Any:
    """Serialize a live ``BaseWorkflow`` body, keeping declared paths."""
    if not isinstance(body, BaseModel):
        return body
    document: dict[str, Any] = body.model_dump(mode="json")
    tasks: list[BaseTask] = getattr(body, "tasks", [])
    roots: list[BaseArtifact] = getattr(body, "artifacts", [])
    _restore_declared_paths(_entries(document, "artifacts"), roots)
    for entry, task in zip(_entries(document, "tasks"), tasks, strict=False):
        _restore_declared_paths(entry.get("inputs") or [], list(task.inputs))
        _restore_declared_paths(entry.get("outputs") or [], list(task.outputs))
    return document


def _entries(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return the ``key`` list of a body document as plain dicts."""
    raw = body.get(key) or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def derive_ports(
    body: dict[str, Any],
    port_overrides: dict[str, str] | None = None,
) -> tuple[list[SubworkflowPort], list[SubworkflowPort]]:
    """
    Derive a subworkflow body's input and output ports.

    This is the single definition of a subworkflow's interface, shared by
    the load-time validator, the runtime expansion, and any other consumer
    (a UI, or a future port of this rule to TypeScript). Nothing is
    declared twice: the protocol lives in the child workflow document
    itself.

    Args:
        body: A complete ``BaseWorkflow`` document.
        port_overrides: Optional ``derived_name -> new_name`` renames,
            applied last.

    Returns:
        ``(in_ports, out_ports)``. In-ports are the body's root artifacts
        (``body["artifacts"]``), which inner edges reference as
        ``artifact-<rootId>``. Out-ports are the task outputs no inner edge
        consumes, named by artifact id and qualified to ``taskid.artifactid``
        when two of them share an id.
    """
    overrides = port_overrides or {}

    def rename(name: str) -> str:
        return overrides.get(name, name)

    in_ports = [
        SubworkflowPort(name=rename(str(art["id"])), artifact=str(art["id"]))
        for art in _entries(body, "artifacts")
        if "id" in art
    ]

    consumed = {
        (str(edge.get("source")), str(edge.get("source_output")))
        for edge in _entries(body, "edges")
    }
    leaves = [
        (str(task["id"]), str(out["id"]))
        for task in _entries(body, "tasks")
        if "id" in task
        for out in (task.get("outputs") or [])
        if isinstance(out, dict)
        and "id" in out
        and (str(task["id"]), str(out["id"])) not in consumed
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


class SubworkflowExpander(HorusTask):
    """
    Inlines a complete child workflow into the parent's live DAG.

    See the module docstring for port derivation and boundary rewiring.
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

    body: dict[str, Any]
    """
    A complete, valid ``BaseWorkflow`` document. Any existing workflow can
    be dropped in unchanged: its ports are derived from it (see
    :func:`derive_ports`), so nothing about it has to be adapted first.
    """

    port_overrides: dict[str, str] = Field(default_factory=dict)
    """Optional ``derived_name -> new_name`` renames for derived ports."""

    max_depth: int = 10
    """
    Maximum nesting depth, counted from the ``/``-separated inner-id prefix.
    Guards against a body that (transitively) embeds itself.
    """

    @model_validator(mode="before")
    @classmethod
    def _accept_live_workflow(cls, data: Any) -> Any:
        """
        Accept a live ``BaseWorkflow`` as ``body`` and serialize it.

        Dropping an existing workflow in should not require the author to
        decide how to dump it; see :func:`_as_document` for why a plain
        ``model_dump`` is not enough.
        """
        if isinstance(data, dict) and isinstance(data.get("body"), BaseModel):
            return {**data, "body": _as_document(data["body"])}
        return data

    @model_validator(mode="after")
    def _ensure_ports(self) -> Self:
        """
        Validate the body and append one placeholder artifact per derived
        port, idempotently (so a dumped-and-reloaded expander does not
        duplicate them).

        The body is checked by simply constructing a ``BaseWorkflow`` from
        it, which reuses every existing workflow validation (unique ids,
        edges resolve, no cycles) instead of reimplementing any of it. This
        matters at load time, not only at run time: a UI must be able to
        reject a malformed subworkflow when it is saved rather than
        halfway through a run.
        """
        # Local import: base.py imports this module at import time.
        from horus_runtime.core.workflow.base import (  # noqa: PLC0415
            BaseWorkflow,
        )

        for entry in _entries(self.body, "tasks"):
            inner_id = str(entry.get("id", ""))
            if _ID_SEPARATOR in inner_id:
                raise SubworkflowError(
                    _(
                        "Subworkflow '%(id)s' body task id '%(inner)s' may "
                        "not contain '/': it is reserved for the inlined "
                        "id prefix."
                    )
                    % {"id": self.id, "inner": inner_id}
                )

        BaseWorkflow.model_validate(self.body)

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
        one atomic ``wf.expand(...)``. See the module docstring.
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

        for raw in _entries(self.body, "edges"):
            source = str(raw.get("source"))
            if source.startswith(_ROOT_PREFIX):
                root = str(raw.get("source_output"))
                self._wire_in_port(root, raw, feeds, by_id, edges, artifacts)
            else:
                edges.append(
                    WorkflowEdge(
                        source=self._prefixed(source),
                        source_output=raw.get("source_output"),
                        target=self._prefixed(str(raw.get("target"))),
                        target_input=raw.get("target_input"),
                        transfer=bool(raw.get("transfer", True)),
                        condition=self._prefixed_condition(
                            raw.get("condition")
                        ),
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
        if not isinstance(condition, dict):
            return condition
        source_task = condition.get("source_task")
        if not isinstance(source_task, str):
            return condition
        return {**condition, "source_task": self._prefixed(source_task)}

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
        for entry in _entries(self.body, "tasks"):
            inner_id = str(entry["id"])
            if _ID_SEPARATOR in inner_id:
                raise SubworkflowError(
                    _(
                        "Subworkflow '%(id)s' body task id '%(inner)s' may "
                        "not contain '/'."
                    )
                    % {"id": self.id, "inner": inner_id}
                )
            data: dict[str, Any] = {**entry}
            data.setdefault("kind", "horus_task")
            data["id"] = self._prefixed(inner_id)
            data["name"] = data["id"]
            # A forced re-run of the expander (e.g. CLI --no-skip-all)
            # must reach the tasks it inlines.
            if not self.skip_if_complete:
                data["skip_if_complete"] = False
            task = BaseTask.model_validate(data)
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
        self, wf: "BaseWorkflow", port: SubworkflowPort
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
        raw: dict[str, Any],
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
        target = self._prefixed(str(raw.get("target")))
        target_input = raw.get("target_input")
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
                (
                    a
                    for a in _entries(self.body, "artifacts")
                    if str(a.get("id")) == root
                ),
                None,
            )
            if entry is None:
                raise SubworkflowError(
                    _(
                        "Subworkflow '%(id)s' body references unknown root "
                        "artifact '%(root)s'."
                    )
                    % {"id": self.id, "root": root}
                )
            artifacts.append(
                BaseArtifact.model_validate({**entry, "id": prefixed_root})
            )
        edges.append(
            WorkflowEdge(
                source=f"{_ROOT_PREFIX}{prefixed_root}",
                source_output=prefixed_root,
                target=target,
                target_input=target_input,
                transfer=bool(raw.get("transfer", True)),
                condition=self._prefixed_condition(raw.get("condition")),
            )
        )

    def _out_port_edges(
        self, wf: "BaseWorkflow", out_ports: list[SubworkflowPort]
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


def lower_subworkflow_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """
    Lower one raw YAML task-dict carrying a ``sub:`` block into a
    ``kind: subworkflow`` task-dict.

    Because ports are derived from the body, the sugar is just the child
    workflow written inline — there is no port or binding block to author.

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


def subworkflow_task(
    wf: "BaseWorkflow",
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

    Mirrors the YAML ``sub:`` block (see :func:`lower_subworkflow_entry`),
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
    # Boundary edges never carry data themselves (see the module
    # docstring); force any that already exist, as the YAML lowering does.
    for edge in wf.edges:
        if id in (edge.source, edge.target):
            edge.transfer = False
    return expander
