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
Base Workflow. The workflow orchestrates an ordered set of tasks, using
artifact existence and integrity to determine which tasks need to run.

Each task declares its output artifacts. A task is skipped if all of its
outputs already exist, because the workflow treats output artifact presence
as proof of prior successful completion. Any task with no declared outputs
always runs unconditionally.

Task ordering and dependency resolution are delegated to the concrete
workflow implementation. For example, :class:`HorusWorkflow` resolves
dependencies from the workflow's explicit edges and executes tasks in
topological (DAG) order, which may differ from the order they are defined.
"""

from abc import abstractmethod
from asyncio import CancelledError
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar, Literal, NamedTuple, Self, final
from uuid import UUID, uuid4

import yaml
from pydantic import Field, PrivateAttr, model_validator

from horus_builtin.workflow.branch import BranchRouter, branch_task
from horus_builtin.workflow.dag import (
    CyclicDependencyError,
    build_dependencies,
    topological_sort,
    would_create_cycle,
)
from horus_builtin.workflow.loop import (
    LoopController,
    loop_task,
    lower_loop_entry,
)
from horus_builtin.workflow.map import MapExpander, lower_map_entry, map_task
from horus_runtime.context import HorusContext, current_task_id
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.placement import ResourceCapacity
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.transfer.exceptions import (
    OrchestratorTargetNotSetError,
)
from horus_runtime.core.transfer.generic import GenericTransfer
from horus_runtime.core.transfer.strategy import BaseTransferStrategy
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import (
    ArtifactIdsAreNotUniqueError,
    DuplicateEdgeTargetError,
    OneWorkflowAtATimeError,
    TaskIdsAreNotUniqueError,
    UnknownEdgeEndpointError,
)
from horus_runtime.core.workflow.status import WorkflowStatus
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.middleware.workflow import (
    WorkflowMiddleware,
    WorkflowMiddlewareContext,
)
from horus_runtime.registry.auto_registry import AutoRegistry


class _EdgeSource(NamedTuple):
    """
    Where a consumer input is sourced from, resolved from the workflow edges.

    ``target`` is the producer task's target, or ``None`` for a root source
    (which comes from the orchestrator). ``artifact`` is the producing
    artifact (a task output or a root artifact); it carries the id the data is
    stored under, which differs from the consumer input id.
    """

    target: BaseTarget | None
    artifact: BaseArtifact | None


class BaseWorkflow(AutoRegistry, entry_point="workflow"):
    """
    Orchestrates an ordered collection of tasks.
    """

    registry_key: ClassVar[str] = "kind"

    kind: str
    """
    The 'kind' field is used to identify the specific type of workflow.
    """

    kind_name: ClassVar[str] = "BaseWorkflow"
    """
    Human-readable name for this workflow type, used in the UI.
    """

    kind_description: ClassVar[str] = _("Horus base workflow")
    """
    Description of this workflow type, used in the UI.
    """

    id: UUID = Field(default_factory=uuid4)
    """
    Unique identifier for this workflow instance.
    """

    name: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9 _-]+$")
    """
    Human-readable name for this workflow.
    """

    tasks: list[BaseTask] = Field(
        default_factory=list,
    )
    """
    List of task instances.
    """

    artifacts: list[BaseArtifact] = Field(
        default_factory=list,
    )
    """
    Standalone root artifacts (no producer task). Tasks can reference these
    by connecting their input artifact IDs to a root artifact's ID.
    """

    edges: list[WorkflowEdge] = Field(
        default_factory=list,
    )
    """
    Explicit connections between producer outputs and consumer inputs. These
    are the sole source of truth for the DAG and the artifact transfer sources.
    A workflow with no edges has independent tasks (no dependencies).
    """

    orchestrator_target: BaseTarget | None = None
    """
    The target that represents the orchestrator (the machine running the
    workflow itself). Used as the transfer source for root input artifacts:
    those not produced by any upstream task. Must be set when the workflow
    dispatches tasks to remote targets that cannot directly access local
    artifacts.
    """

    status: WorkflowStatus = WorkflowStatus.IDLE
    """
    Current execution state of the workflow. Updated automatically by
    ``run()``; do not set manually.
    """

    max_concurrency: int | None = None
    """
    Upper bound on the number of tasks the scheduler dispatches at once.
    ``None`` (the default) means unbounded: every task that becomes ready is
    dispatched immediately. Set this to cap resource usage (e.g. a shared
    machine with limited CPUs) when the DAG's natural parallelism would
    otherwise over-subscribe it.
    """

    capacity: dict[str, ResourceCapacity] | None = None
    """
    Optional, opt-in compute capacity declared per ``location_id`` (see
    :attr:`~horus_runtime.core.target.base.BaseTarget.location_id`). When
    set, the scheduler gates dispatch of any ready task that declares
    ``resources`` so concurrent tasks sharing a location never request more
    of a dimension than is available there (e.g. at most 2 GPU-requesting
    tasks running at once on a location capped at ``gpus=2``).

    ``None`` (the default) or an empty map means no capacity is declared
    anywhere: every task dispatches exactly as it did before this field
    existed, governed only by ``max_concurrency``. A location absent from
    the map, or a task with ``resources=None``, is likewise never gated.
    """

    failure_policy: Literal["fail_fast", "continue"] = "fail_fast"
    """
    How the scheduler reacts to a task failure.

    - ``"fail_fast"`` (the default): the first task to fail cancels every
      other task still in flight and the run stops immediately, matching the
      runtime's historical behavior.
    - ``"continue"``: a failed task does not abort the run. Its descendants
      are never dispatched (they permanently lack a satisfied dependency),
      but every other branch of the DAG keeps running to completion. Once
      nothing more can run, the workflow still ends ``FAILED`` if any task
      failed, naming every failed task.

    Either way a task failure always results in a ``FAILED`` workflow; the
    policy only controls how much of the DAG gets to run first.
    """

    _base_directory: Path | None = PrivateAttr(default=None)
    """
    Directory relative paths are anchored to (the run directory and, through
    it, artifact paths and logs). Set to the workflow YAML's folder by
    :meth:`from_yaml`; ``None`` for programmatically-built workflows, which
    fall back to the process CWD. Runtime-only state, not serialized.
    """

    _implicit_task_deps: dict[str, set[str]] = PrivateAttr(
        default_factory=dict
    )
    """
    Runtime-only ordering dependencies not expressed by any edge:
    ``new_task_id -> {creator_task_id}``. Populated when
    :meth:`add_task`/:meth:`expand` is called from inside a running task and
    the new task has no incoming task edge of its own — it is gated behind its
    creator so a task generated mid-run (e.g. by a ``plan`` step expanding the
    DAG) runs after the step that created it, without the caller having to
    invent a placeholder edge. Merged into the scheduler's dependency map
    (see :func:`horus_builtin.workflow.scheduler.run_schedule`) alongside the
    edge-derived deps. Never serialized.
    """

    _revision: int = PrivateAttr(default=0)
    """
    Monotonically increasing counter for the workflow's DAG structure
    (tasks/edges/artifacts). Nothing bumps it yet: today the DAG is fixed for
    the lifetime of a run, so it is built once and stays at ``0``. It exists
    so a future dynamic-DAG feature (fan-out/map/loops) can increment it when
    mutating the graph mid-run, and the scheduler's cached source map (see
    :meth:`cached_source_map`) picks up the change without any changes to the
    scheduler itself.
    """

    @model_validator(mode="before")
    @classmethod
    def _lower_map_tasks(cls, data: object) -> object:
        """
        Lower any task carrying a ``map:`` block into a ``map_expander``
        task plus its construction-time wiring edge, before normal
        per-task ``kind``-discriminated parsing runs.

        See :func:`horus_builtin.workflow.map.lower_map_entry`. A no-op for
        workflows with no ``map:`` tasks, including already-lowered,
        round-tripped ones (``to_yaml`` dumps a ``MapExpander`` in its
        native ``kind: map_expander`` form, not back into ``map:`` block
        syntax) and direct Python construction with real task objects.
        """
        if not isinstance(data, dict):
            return data
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            return data
        if not any(isinstance(t, dict) and "map" in t for t in tasks):
            return data

        new_tasks: list[object] = []
        new_edges: list[object] = list(data.get("edges") or [])
        for entry in tasks:
            if isinstance(entry, dict) and "map" in entry:
                expander, edges = lower_map_entry(entry)
                new_tasks.append(expander)
                new_edges.extend(edges)
            else:
                new_tasks.append(entry)

        return {**data, "tasks": new_tasks, "edges": new_edges}

    def map(
        self,
        *,
        id: str,
        template: BaseTask,
        gather: tuple[str, str],
        over: tuple[str, str, str] | None = None,
        range: int | None = None,
        index_input: str | None = None,
        name: str | None = None,
        target: BaseTarget | None = None,
    ) -> MapExpander:
        """
        Append a declarative map (fan-out/fan-in) task to this workflow.

        Thin delegate to :func:`horus_builtin.workflow.map.map_task`; see
        its docstring for the full parameter reference. Equivalent to
        authoring a ``map:`` block in YAML (see
        :func:`horus_builtin.workflow.map.lower_map_entry`).
        """
        return map_task(
            self,
            id=id,
            template=template,
            gather=gather,
            over=over,
            range=range,
            index_input=index_input,
            name=name,
            target=target,
        )

    @model_validator(mode="before")
    @classmethod
    def _lower_loop_tasks(cls, data: object) -> object:
        """
        Lower any task carrying a ``loop:`` block into a ``loop_controller``
        task, before normal per-task ``kind``-discriminated parsing runs.

        See :func:`horus_builtin.workflow.loop.lower_loop_entry`. A no-op
        for workflows with no ``loop:`` tasks, including already-lowered,
        round-tripped ones (``to_yaml`` dumps a ``LoopController`` in its
        native ``kind: loop_controller`` form, not back into ``loop:``
        block syntax) and direct Python construction with real task
        objects.
        """
        if not isinstance(data, dict):
            return data
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            return data
        if not any(isinstance(t, dict) and "loop" in t for t in tasks):
            return data

        new_tasks: list[object] = [
            lower_loop_entry(entry)
            if isinstance(entry, dict) and "loop" in entry
            else entry
            for entry in tasks
        ]

        return {**data, "tasks": new_tasks}

    def loop(
        self,
        *,
        id: str,
        body: BaseTask,
        until: str,
        max_iterations: int,
        index_input: str | None = None,
        name: str | None = None,
        target: BaseTarget | None = None,
    ) -> LoopController:
        """
        Append a declarative conditional-repeat loop task to this workflow.

        Thin delegate to :func:`horus_builtin.workflow.loop.loop_task`; see
        its docstring for the full parameter reference. Equivalent to
        authoring a ``loop:`` block in YAML (see
        :func:`horus_builtin.workflow.loop.lower_loop_entry`).
        """
        return loop_task(
            self,
            id=id,
            body=body,
            until=until,
            max_iterations=max_iterations,
            index_input=index_input,
            name=name,
            target=target,
        )

    def branch(
        self,
        *,
        id: str,
        func: Callable[..., str | list[str]],
        routes: list[str],
        name: str | None = None,
        target: BaseTarget | None = None,
    ) -> BranchRouter:
        """
        Append a switch-style branch (a router task plus one gated edge per
        route) to this workflow.

        Thin delegate to :func:`horus_builtin.workflow.branch.branch_task`;
        see its docstring for the full parameter reference. There is no
        equivalent YAML block, because a branch lowers to conditioned edges,
        which YAML can already author directly (see the module docstring of
        :mod:`horus_builtin.workflow.branch`).
        """
        return branch_task(
            self,
            id=id,
            func=func,
            routes=routes,
            name=name,
            target=target,
        )

    @staticmethod
    def _assert_unique_task_ids(tasks: list[BaseTask]) -> None:
        """
        Raise :exc:`TaskIdsAreNotUniqueError` if any two tasks in *tasks*
        share an id. Shared by the construction-time validator
        (:meth:`check_unique_task_ids`) and the incremental mutators
        (:meth:`add_task`, :meth:`expand`) so both paths enforce the same
        rule.
        """
        seen_ids: set[str] = set()
        for task in tasks:
            if task.id in seen_ids:
                raise TaskIdsAreNotUniqueError(task.id)
            seen_ids.add(task.id)

    @staticmethod
    def _assert_unique_artifact_ids(artifacts: list[BaseArtifact]) -> None:
        """
        Raise :exc:`ArtifactIdsAreNotUniqueError` if any two artifacts in
        *artifacts* share an id. Shared by the construction-time validator
        (:meth:`check_unique_artifact_ids`) and the incremental mutators
        (:meth:`add_artifact`, :meth:`add_task`, :meth:`expand`) so both
        paths enforce the same rule.
        """
        seen_ids: set[str] = set()
        for artifact in artifacts:
            if artifact.id in seen_ids:
                raise ArtifactIdsAreNotUniqueError(artifact.id)
            seen_ids.add(artifact.id)

    @model_validator(mode="after")
    def check_unique_task_ids(self) -> Self:
        """
        Validates that all tasks have unique ids. This is required for correct
        dependency resolution and execution.
        """
        self._assert_unique_task_ids(self.tasks)
        return self

    @model_validator(mode="after")
    def check_unique_artifact_ids(self) -> Self:
        """
        Validates artifact id uniqueness where edge resolution requires it:
        output ids must be unique *within each task*, and root artifact ids
        unique among roots. Output ids may repeat across tasks, edges resolve
        on ``(task id, output id)`` and task ids are unique, so the same
        reusable task can be placed more than once.
        """
        # Root artifacts must have unique ids across the workflow.
        self._assert_unique_artifact_ids(self.artifacts)

        # Output and input artifacts must have unique ids within each task.
        for task in self.tasks:
            self._assert_unique_artifact_ids(task.outputs)
            self._assert_unique_artifact_ids(task.inputs)

        return self

    @staticmethod
    def _assert_edge_target_resolves(
        edge: WorkflowEdge,
        task_inputs: dict[str, set[str]],
    ) -> None:
        """
        Raise :exc:`UnknownEdgeEndpointError` unless *edge* targets a real
        task and one of its declared input ids. Shared by
        :meth:`check_edges_resolve`, :meth:`add_edge`, and :meth:`expand`.

        An edge naming no artifacts only has to target a real task: it exists
        to order tasks that may declare no inputs at all.
        """
        if edge.target not in task_inputs:
            raise UnknownEdgeEndpointError("target task", edge.target)
        if edge.target_input is None:
            return
        if edge.target_input not in task_inputs[edge.target]:
            raise UnknownEdgeEndpointError("target input", edge.target_input)

    @staticmethod
    def _assert_edge_source_resolves(
        edge: WorkflowEdge,
        task_outputs: dict[str, set[str]],
        root_ids: set[str],
    ) -> None:
        """
        Raise :exc:`UnknownEdgeEndpointError` unless *edge* sources a real
        task output, or a root artifact via the ``artifact-<rootId>``
        convention. Shared by :meth:`check_edges_resolve`, :meth:`add_edge`,
        and :meth:`expand`.

        An edge naming no artifacts only has to source a real task: it exists
        to order tasks that may declare no outputs at all. A root artifact is
        not a task and so has nothing to order against, which leaves it
        rejected by the final branch.
        """
        if edge.source in task_outputs:
            if (
                edge.source_output is not None
                and edge.source_output not in task_outputs[edge.source]
            ):
                raise UnknownEdgeEndpointError(
                    "source output", edge.source_output
                )
        elif edge.source.startswith("artifact-"):
            if (
                edge.source_output is None
                or edge.source_output not in root_ids
            ):
                raise UnknownEdgeEndpointError(
                    "root artifact", str(edge.source_output)
                )
        else:
            raise UnknownEdgeEndpointError("source task", edge.source)

    @model_validator(mode="after")
    def check_edges_resolve(self) -> Self:
        """
        Validate that every edge references real endpoints.

        Edges are the sole source of truth for the DAG and for transfer
        sources, so an unresolved endpoint (typo, stale reference) would
        silently drop a dependency or misroute a transfer. Each edge must:

        - target an existing task and one of its declared input ids;
        - source either an existing task's declared output id, or a root
          artifact id via the ``artifact-<rootId>`` convention;
        - be the only ``transfer=True`` edge feeding its
          ``(target, target_input)``. Ordering-only (``transfer=False``)
          edges are exempt: any number of them may feed the same input,
          alongside at most one ``transfer=True`` edge, since they never
          contribute a transfer source (see :meth:`_build_source_map`).
        """
        task_inputs = {t.id: {a.id for a in t.inputs} for t in self.tasks}
        task_outputs = {t.id: {a.id for a in t.outputs} for t in self.tasks}
        root_ids = {a.id for a in self.artifacts}

        seen_targets: set[tuple[str, str]] = set()
        for edge in self.edges:
            self._assert_edge_target_resolves(edge, task_inputs)

            # At most one transfer edge may feed a given consumer input.
            # Ordering-only edges (transfer=False) are exempt.
            # (`target_input is not None` is implied by `transfer`, and is
            # spelled out to narrow the type.)
            if edge.transfer and edge.target_input is not None:
                key = (edge.target, edge.target_input)
                if key in seen_targets:
                    raise DuplicateEdgeTargetError(
                        edge.target, edge.target_input
                    )
                seen_targets.add(key)

            self._assert_edge_source_resolves(edge, task_outputs, root_ids)

        return self

    # -- Runtime DAG mutation -------------------------------------------
    #
    # The validators above run once, at construction. The methods below let
    # code — typically a task's own body, reached via ``BaseTask.workflow``
    # — grow the live DAG mid-run: add a task, wire an edge, register a root
    # artifact, or commit a whole batch of these atomically. Each performs
    # the same checks the constructor's validators would, but incrementally
    # (against the current graph, not by re-validating the whole model), and
    # bumps ``_revision`` so the scheduler's cached source map (see
    # :meth:`cached_source_map`) picks up the change. The scheduler itself
    # already recomputes dependencies/scope from ``self.tasks``/``self.edges``
    # every loop iteration, so a mutation applied while a task is running
    # takes effect on the next iteration with no further plumbing.

    @property
    def implicit_task_dependencies(self) -> dict[str, set[str]]:
        """
        A copy of the runtime-only ``new_task_id -> {creator_task_id}``
        ordering dependencies (see :attr:`_implicit_task_deps`). The scheduler
        folds these into its edge-derived dependency map every loop iteration.
        """
        return {
            task_id: set(creators)
            for task_id, creators in self._implicit_task_deps.items()
        }

    def _gate_new_tasks_behind_creator(self, new_task_ids: set[str]) -> None:
        """
        Record an implicit ordering dependency from each brand-new task with
        no incoming task edge onto the task that created it (the task running
        in the current context, if any).

        This is what makes a task added mid-run reachable: the scheduler's
        scope is ``ancestors(trigger) | descendants(trigger)``, so a task with
        no path back to the trigger would never run. Gating it behind its
        creator — which is itself in scope — makes it a descendant and orders
        it strictly after the creator completes.

        A no-op outside a running task (static, construction-time building is
        unchanged) and for any new task that already has an incoming task edge
        (e.g. map/loop clones wired to their source), which is already ordered
        by that edge and must not be forced behind the expander instead.
        """
        creator = current_task_id()
        if creator is None:
            return
        # Task ids that are already the target of a task -> task edge: those
        # are ordered by an explicit edge and need no implicit gating.
        task_ids = {task.id for task in self.tasks}
        wired_targets = {
            edge.target for edge in self.edges if edge.source in task_ids
        }
        for task_id in new_task_ids:
            if task_id == creator or task_id in wired_targets:
                continue
            self._implicit_task_deps.setdefault(task_id, set()).add(creator)

    def add_artifact(self, artifact: BaseArtifact) -> None:
        """
        Add a standalone root artifact to the live workflow.

        Incremental version of the root-artifact-id check in
        :meth:`check_unique_artifact_ids`: *artifact*'s id must not collide
        with an existing root artifact id. On success, appends the artifact
        and bumps :attr:`_revision`.

        Raises:
            ArtifactIdsAreNotUniqueError: If ``artifact.id`` collides with an
                existing root artifact id.
        """
        self._assert_unique_artifact_ids([*self.artifacts, artifact])
        self.artifacts.append(artifact)
        self._revision += 1

    def add_task(self, task: BaseTask) -> None:
        """
        Add a task to the live workflow.

        Incremental version of :meth:`check_unique_task_ids` and
        :meth:`check_unique_artifact_ids`: *task*'s id must not collide with
        an existing task id, and its own inputs/outputs must each be unique
        among themselves. The task is then anchored exactly like a
        construction-time task (see :meth:`_anchor_task`: absolute artifact
        paths, local runtime paths, inherited working directory), appended,
        and :attr:`_revision` is bumped.

        Safe to call from inside a running task, e.g.::

            def my_step(task: BaseTask) -> None:
                assert task.workflow is not None
                task.workflow.add_task(new_task)

        The scheduler recomputes scope and dependencies from
        ``self.tasks``/``self.edges`` every loop iteration, so the new task
        is picked up automatically once an edge (see :meth:`add_edge`) makes
        it reachable from the running trigger.

        Raises:
            TaskIdsAreNotUniqueError: If ``task.id`` collides with an
                existing task id.
            ArtifactIdsAreNotUniqueError: If two of the task's own inputs, or
                two of its own outputs, share an id.
        """
        self._assert_unique_task_ids([*self.tasks, task])
        self._assert_unique_artifact_ids(task.outputs)
        self._assert_unique_artifact_ids(task.inputs)

        self.tasks.append(task)
        self._anchor_task(task)
        self._gate_new_tasks_behind_creator({task.id})
        self._revision += 1

    def add_edge(self, edge: WorkflowEdge) -> None:
        """
        Add one edge to the live workflow's DAG.

        Incremental version of :meth:`check_edges_resolve` for a single
        edge: both endpoints must resolve against the current
        tasks/artifacts, no other edge may already feed the same
        ``(target, target_input)``, and the edge must not close a cycle
        (:func:`~horus_builtin.workflow.dag.would_create_cycle`). On
        success, appends the edge and bumps :attr:`_revision`.

        Raises:
            UnknownEdgeEndpointError: If the target task/input, or the
                source task/output/root-artifact, does not exist.
            DuplicateEdgeTargetError: If another edge already feeds
                ``(edge.target, edge.target_input)``.
            CyclicDependencyError: If appending the edge would create a
                cycle.
        """
        task_inputs = {t.id: {a.id for a in t.inputs} for t in self.tasks}
        task_outputs = {t.id: {a.id for a in t.outputs} for t in self.tasks}
        root_ids = {a.id for a in self.artifacts}

        self._assert_edge_target_resolves(edge, task_inputs)

        # At most one transfer edge may feed a given consumer input, exactly
        # as in check_edges_resolve and expand: ordering-only edges never
        # contribute a transfer source, so any number of them may pile onto
        # the same input. (`target_input is not None` is implied by
        # `transfer`, and is spelled out to narrow the type.)
        if edge.transfer and edge.target_input is not None:
            if any(
                e.transfer
                and e.target == edge.target
                and e.target_input == edge.target_input
                for e in self.edges
            ):
                raise DuplicateEdgeTargetError(edge.target, edge.target_input)

        self._assert_edge_source_resolves(edge, task_outputs, root_ids)

        if would_create_cycle(self.edges, edge, self.tasks):
            raise CyclicDependencyError(
                _(
                    "Adding edge from '%(source)s.%(source_output)s' to "
                    "'%(target)s.%(target_input)s' would create a cycle."
                )
                % {
                    "source": edge.source,
                    "source_output": edge.source_output,
                    "target": edge.target,
                    "target_input": edge.target_input,
                }
            )

        self.edges.append(edge)
        self._revision += 1

    def expand(
        self,
        *,
        tasks: list[BaseTask] | None = None,
        edges: list[WorkflowEdge] | None = None,
        artifacts: list[BaseArtifact] | None = None,
    ) -> None:
        """
        Atomically add a batch of tasks, root artifacts, and edges to the
        live workflow.

        Unlike calling :meth:`add_task`/:meth:`add_edge`/:meth:`add_artifact`
        one at a time, the whole batch is validated against the *combined*
        (current + new) graph before anything is committed, so edges within
        the batch may reference tasks/artifacts also being added in the same
        batch (e.g. a fan-out expander adding N mapped tasks plus the edges
        wiring them to an existing join task). If any check fails, nothing
        is appended — the workflow is left exactly as it was.

        Validates, in order: task id uniqueness (existing + new), per-task
        input/output id uniqueness for each new task, root artifact id
        uniqueness (existing + new), every new edge resolving against the
        combined tasks/artifacts with no duplicate ``(target, target_input)``
        (existing or within the batch), and no cycle anywhere in the
        resulting graph.

        On success, every new task is anchored exactly like
        :meth:`add_task` anchors a single task, everything is appended, and
        :attr:`_revision` is bumped once for the whole batch.

        Raises:
            TaskIdsAreNotUniqueError: If a new task's id collides with an
                existing task id or another new task.
            ArtifactIdsAreNotUniqueError: If a new task's own inputs/outputs
                collide, or a new root artifact's id collides with an
                existing or another new root artifact.
            UnknownEdgeEndpointError: If a new edge's target or source does
                not resolve against the combined graph.
            DuplicateEdgeTargetError: If a new edge duplicates the
                ``(target, target_input)`` of an existing or another new
                edge.
            CyclicDependencyError: If the resulting graph contains a cycle.
        """
        new_tasks = tasks or []
        new_edges = edges or []
        new_artifacts = artifacts or []

        combined_tasks = [*self.tasks, *new_tasks]
        combined_artifacts = [*self.artifacts, *new_artifacts]

        self._assert_unique_task_ids(combined_tasks)
        for task in new_tasks:
            self._assert_unique_artifact_ids(task.outputs)
            self._assert_unique_artifact_ids(task.inputs)
        self._assert_unique_artifact_ids(combined_artifacts)

        task_inputs = {t.id: {a.id for a in t.inputs} for t in combined_tasks}
        task_outputs = {
            t.id: {a.id for a in t.outputs} for t in combined_tasks
        }
        root_ids = {a.id for a in combined_artifacts}

        # Only transfer=True edges participate in the "at most one edge per
        # (target, target_input)" rule; ordering-only edges are exempt (see
        # check_edges_resolve), which is what lets a fan-in expander wire
        # many clone -> gather edges onto the same gather input in one
        # batch.
        seen_targets = {
            (e.target, e.target_input) for e in self.edges if e.transfer
        }
        for edge in new_edges:
            self._assert_edge_target_resolves(edge, task_inputs)

            # (`target_input is not None` is implied by `transfer`, and is
            # spelled out to narrow the type.)
            if edge.transfer and edge.target_input is not None:
                key = (edge.target, edge.target_input)
                if key in seen_targets:
                    raise DuplicateEdgeTargetError(
                        edge.target, edge.target_input
                    )
                seen_targets.add(key)

            self._assert_edge_source_resolves(edge, task_outputs, root_ids)

        # A single edge-by-edge would_create_cycle check would miss a cycle
        # formed only by two-or-more new edges together, so validate the
        # whole resulting graph at once: topological_sort raises
        # CyclicDependencyError itself if any cycle exists anywhere in it.
        combined_edges = [*self.edges, *new_edges]
        dependencies = build_dependencies(combined_tasks, combined_edges)
        topological_sort(set(dependencies.keys()), dependencies)

        self.tasks.extend(new_tasks)
        self.artifacts.extend(new_artifacts)
        self.edges.extend(new_edges)
        for task in new_tasks:
            self._anchor_task(task)
        # Runs after edges are appended so a new task wired by one of the
        # batch's own edges (e.g. a map/loop clone, or a fan-in join fed by
        # its producers) is recognised as already-ordered and left ungated.
        self._gate_new_tasks_behind_creator({task.id for task in new_tasks})
        self._revision += 1

    @classmethod
    def from_yaml(cls, path: str | Path) -> Self:
        """
        Load a workflow from a YAML file.

        Args:
            path: Path to the YAML file.

        Returns:
            A fully constructed :class:`BaseWorkflow` instance.
        """
        with Path(path).open("r", encoding="utf-8") as fh:
            workflow = cls.model_validate(yaml.safe_load(fh))
        # Anchor the run (working dirs, outputs, logs) to the workflow file's
        # own directory, so a run is self-contained regardless of the launch
        # directory.
        workflow._base_directory = Path(path).resolve().parent  # noqa: SLF001
        return workflow

    def to_yaml(self, path: str | Path) -> None:
        """
        Save the workflow to a YAML file.

        Dumps in ``mode="json"``: PyYAML's ``safe_dump`` cannot represent
        arbitrary Python objects (e.g. the ``pathlib.Path`` every artifact
        carries), so fields are coerced to YAML-safe primitives (paths and
        similar become plain strings) exactly as ``from_yaml`` expects them
        back on load.

        Args:
            path: Path to the YAML file.

        Returns:
            None
        """
        with Path(path).open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self.model_dump(mode="json"), fh)

    def _build_source_map(self) -> dict[tuple[str, str], _EdgeSource]:
        """
        Resolve, for the whole workflow, where each consumer input is sourced.

        Returns a map keyed by (target task id, input id) to an
        :class:`_EdgeSource`. Edges are validated at construction
        (see :meth:`check_edges_resolve`), so every entry resolves to a real
        producer output or root artifact.

        Only ``transfer=True`` edges contribute an entry: ordering-only
        (``transfer=False``) edges affect DAG ordering (see
        :func:`horus_builtin.workflow.dag.build_dependencies`) but never
        supply a transfer source.
        """
        targets_by_task = {t.id: t.target for t in self.tasks}
        outputs_by_task = {
            (t.id, o.id): o for t in self.tasks for o in t.outputs
        }
        roots_by_id = {a.id: a for a in self.artifacts}

        source_map: dict[tuple[str, str], _EdgeSource] = {}
        for edge in self.edges:
            # (The id checks are implied by `transfer`, and are spelled out to
            # narrow the types for the key and lookups below.)
            if (
                not edge.transfer
                or edge.target_input is None
                or edge.source_output is None
            ):
                continue
            key = (edge.target, edge.target_input)
            producer_target = targets_by_task.get(edge.source)
            if producer_target is not None:
                # Task source: producer's target + its output artifact.
                source_map[key] = _EdgeSource(
                    producer_target,
                    outputs_by_task.get((edge.source, edge.source_output)),
                )
            else:
                # Root source ("artifact-<id>"): sourced from orchestrator.
                source_map[key] = _EdgeSource(
                    None, roots_by_id.get(edge.source_output)
                )
        return source_map

    def cached_source_map(
        self,
        cached: tuple[int, dict[tuple[str, str], _EdgeSource]] | None,
    ) -> tuple[int, dict[tuple[str, str], _EdgeSource]]:
        """
        Return ``(self._revision, source_map)``, rebuilding the source map
        only when ``_revision`` has advanced past *cached*.

        Intended for callers (namely the scheduler) that need the source map
        across several iterations of a run: pass back the tuple you got last
        time and only a genuinely stale cache triggers a rebuild via
        :meth:`_build_source_map`. Passing ``None`` always rebuilds.
        """
        if cached is not None and cached[0] == self._revision:
            return cached
        return self._revision, self._build_source_map()

    async def transfer_artifacts(
        self,
        task: BaseTask,
        source_map: dict[tuple[str, str], _EdgeSource] | None = None,
    ) -> None:
        """
        Transfer the input artifacts of the given task to the target where the
        task will run, using the appropriate transfer strategies.

        Called by the workflow before dispatching a task to ensure all inputs
        are available on the task's target.

        The source target for each input artifact is resolved as follows:

        1. If a workflow edge feeds the input from another task's output, that
           producer task's target is used as the source.
        2. Otherwise (no edge, or the edge's source is a root artifact) the
           input is treated as a root input (user-provided) and
           ``self.orchestrator_target`` is used as the source. If
           ``orchestrator_target`` is ``None`` a
           :exc:`OrchestratorTargetNotSetError` is raised.

        Args:
            task: The task whose input artifacts should be transferred.
            source_map: Precomputed edge source map (see
                :meth:`_build_source_map`). Built on demand when omitted;
                ``_run`` builds it once and passes it for every task.

        Raises:
            OrchestratorTargetNotSetError: When a root artifact cannot be
                accessed by the destination and no orchestrator_target is set.
        """
        if source_map is None:
            source_map = self._build_source_map()

        for artifact in task.inputs:
            source = source_map.get((task.id, artifact.id))
            # Resolve the source target.
            source_target = source.target if source is not None else None
            if source_target is None:
                # Root input: must come from the orchestrator.
                if self.orchestrator_target is None:
                    raise OrchestratorTargetNotSetError(
                        artifact.id, task.target
                    )
                source_target = self.orchestrator_target

            # Transfer a copy of the *producing* artifact: it carries the id
            # the data is stored under (so the lookup hits) and source path.
            # The consumer input keeps its own id (the template key); we only
            # point its path at the materialized result.
            src_artifact = source.artifact if source is not None else None
            transfer_art = (src_artifact or artifact).model_copy()

            # Look up the registered strategy for this (source, dest) pair,
            # falling back to the target-agnostic GenericTransfer when no
            # location-specific strategy is registered.
            strategy_cls = BaseTransferStrategy.get_from_registry(
                source_target, task.target
            )
            strategy = (
                strategy_cls()
                if strategy_cls is not None
                else GenericTransfer()
            )

            horus_logger.log.debug(
                _(
                    "Transferring artifact '%(id)s' from '%(src)s'"
                    " to '%(dst)s' via %(strategy)s."
                )
                % {
                    "id": transfer_art.id,
                    "src": source_target.kind,
                    "dst": task.target.kind,
                    "strategy": type(strategy).__name__,
                }
            )

            # Perform the transfer, then point the consumer input at the
            # materialized location so its body templating resolves correctly.
            await strategy.transfer(transfer_art, source_target, task.target)
            artifact.path = transfer_art.path

    @property
    def _effective_base(self) -> Path:
        return self._base_directory or Path.cwd()

    @property
    def run_directory(self) -> Path:
        """
        The single root directory for this run's generated files: per-task
        working directories, declared output artifacts, and logs all live
        under it, so a run is self-contained.

        Resolved as ``base_directory / orchestrator working_directory`` (the
        base directory alone when no orchestrator working directory is set).
        The base directory is the workflow YAML's folder when loaded via
        :meth:`from_yaml`, otherwise the process CWD. An absolute orchestrator
        working directory is used as-is.
        """
        base = self._effective_base
        wd = (
            self.orchestrator_target.working_directory
            if self.orchestrator_target is not None
            else None
        )
        root = Path(wd) if wd else Path(".")
        return (base / root).resolve()

    def _produced_declared_paths(self) -> set[Path]:
        """
        Declared paths produced by some task's output, used to decide whether
        a declared artifact path anchors under the run root (produced) or the
        base directory (external, never produced by this workflow).
        """
        return {
            artifact.declared_path
            for task in self.tasks
            for artifact in task.outputs
            if artifact.declared_path is not None
        }

    @staticmethod
    def _anchor_artifact(
        artifact: BaseArtifact,
        *,
        base: Path,
        run_root: Path,
        produced: set[Path],
    ) -> None:
        """
        Make *artifact*'s declared path absolute, rooted at *run_root* when
        it is produced by some task, or *base* otherwise. A path declared
        absolute is left untouched. Only relative declared paths are
        rewritten, so calling this more than once for the same artifact is
        safe.
        """
        declared = artifact.declared_path
        if declared is None or declared.is_absolute():
            return
        root = run_root if declared in produced else base
        artifact.path = (root / declared).resolve()

    def _anchor_task(self, task: BaseTask) -> None:
        """
        Anchor one task's declared artifact paths, local runtime paths, and
        working directory to this workflow's run layout.

        This is the single code path both construction-time tasks (via
        :meth:`_resolve_run_paths` and
        :meth:`_propagate_orchestrator_working_directory`, each called once
        from :meth:`run`) and runtime-added tasks (via :meth:`add_task` /
        :meth:`expand`) go through, so a task added mid-run is anchored
        identically to one declared up front:

        - Declared input/output artifact paths become absolute, rooted under
          :attr:`run_directory` for paths some task produces, or the base
          directory for external paths (see :meth:`_anchor_artifact`).
        - ``task.runtime.anchor_local_paths(base)`` resolves any relative
          local files the runtime owns (e.g. a script path).
        - A co-located task target (same ``location_id`` as the orchestrator
          target) that has not set its own ``working_directory`` inherits the
          orchestrator target's working directory, mirroring what
          :meth:`_propagate_orchestrator_working_directory` used to do
          inline.

        Every step here only rewrites still-relative/unset state, so calling
        this more than once for the same task is safe.
        """
        base = self._effective_base
        run_root = self.run_directory
        produced = self._produced_declared_paths()

        for artifact in (*task.inputs, *task.outputs):
            self._anchor_artifact(
                artifact, base=base, run_root=run_root, produced=produced
            )
        task.runtime.anchor_local_paths(base)

        if self.orchestrator_target is not None:
            orchestrator_wd = self.orchestrator_target.working_directory
            if orchestrator_wd is not None:
                target = task.target
                if (
                    target.working_directory is None
                    and target.location_id
                    == self.orchestrator_target.location_id
                ):
                    target.working_directory = orchestrator_wd

    @final
    def _resolve_run_paths(self) -> Path:
        """
        Anchor this run's relative paths to the single :attr:`run_directory`.

        - Point the orchestrator target at the absolute run directory, so
          per-task working dirs nest under it.
        - Anchor every task (see :meth:`_anchor_task`): declared artifact
          paths, local runtime paths, and co-located working-directory
          inheritance.
        - Anchor standalone root artifacts the same way as task artifacts.

        Only relative declared paths are rewritten, so calling this twice is
        safe. Returns the resolved run root so callers can use it directly.
        """
        base = self._effective_base
        run_root = self.run_directory

        if self.orchestrator_target is not None:
            self.orchestrator_target.working_directory = run_root.as_posix()

        for task in self.tasks:
            self._anchor_task(task)

        produced = self._produced_declared_paths()
        for artifact in self.artifacts:
            self._anchor_artifact(
                artifact, base=base, run_root=run_root, produced=produced
            )

        return run_root

    @final
    def _propagate_orchestrator_working_directory(self) -> None:
        """
        Give every co-located task target the orchestrator target's working
        directory as its base, so local tasks run inside the orchestrator's
        folder (each still nested under its own ``working_dir / task.id``).

        A target is co-located with the orchestrator when it shares its
        ``location_id`` (same filesystem). A task target that already has a
        ``working_directory`` set (not ``None``) is left untouched. Delegates
        to :meth:`_anchor_task` per task (see its docstring); when called
        after :meth:`_resolve_run_paths` (as :meth:`run` does) this is a
        no-op, since every task was already anchored.
        """
        if self.orchestrator_target is None:
            return
        if self.orchestrator_target.working_directory is None:
            return
        for task in self.tasks:
            self._anchor_task(task)

    async def run(self, trigger_id: str) -> None:
        """
        Execute the workflow, managing status transitions automatically.

        Subclasses must implement ``_run()`` instead of overriding this method.
        Status is driven entirely here:

        - ``RUNNING``   — set immediately on entry
        - ``COMPLETED`` — set on clean exit
        - ``CANCELED``  — set when ``CancelledError`` is raised
        - ``FAILED``    — set on any other exception (re-raised after)
        """
        # Set the context workflow to self for the duration of the run
        # so that tasks can access it.
        ctx = HorusContext.get_context()

        # Only a single workflow run is allowed (for now)
        if ctx.workflow is not None:
            raise OneWorkflowAtATimeError(ctx.workflow)

        ctx.workflow = self

        # Anchor working dirs, artifact paths, and logs to a single
        # self-contained run directory before anything reads them, including
        # the RUNNING log below (so it lands in the file sink too).
        run_root = self._resolve_run_paths()
        horus_logger.set_log_directory(run_root / "logs")

        self.status = WorkflowStatus.RUNNING
        horus_logger.log.debug(
            _("Workflow %(workflow_name)s status → RUNNING")
            % {"workflow_name": self.name}
        )

        # Co-located task targets inherit the orchestrator's working
        # directory so all such tasks run under that common folder.
        self._propagate_orchestrator_working_directory()

        try:
            # Wrap the function to pass the trigger.
            def call_run() -> Awaitable[None]:
                return self._run(trigger_id)

            await WorkflowMiddleware.call_with_middleware(
                WorkflowMiddlewareContext(workflow=self),
                call_run,
            )
        except CancelledError:
            self.status = WorkflowStatus.CANCELED
            horus_logger.log.debug(
                _("Workflow %(workflow_name)s status → CANCELED")
                % {"workflow_name": self.name}
            )
            raise
        except Exception:
            self.status = WorkflowStatus.FAILED
            horus_logger.log.debug(
                _("Workflow %(workflow_name)s status → FAILED")
                % {"workflow_name": self.name}
            )
            raise
        else:
            self.status = WorkflowStatus.COMPLETED
            horus_logger.log.debug(
                _("Workflow %(workflow_name)s status → COMPLETED")
                % {"workflow_name": self.name}
            )
        finally:
            # Clear the context workflow on exit
            ctx.workflow = None

    @abstractmethod
    async def _run(self, trigger_id: str) -> None:
        """
        Workflow-specific execution logic. Implement this in subclasses.
        Do not set ``self.status`` here; ``run()`` manages it.
        """

    @final
    async def reset(self) -> None:
        """
        Reset the workflow by deleting all output artifacts of all tasks in the
        workflow. This allows the workflow to be re-run from scratch.
        """
        self.status = WorkflowStatus.IDLE
        horus_logger.log.debug(
            _("Resetting workflow %(workflow_name)s.")
            % {"workflow_name": self.name}
        )
        await self._reset()

    @abstractmethod
    async def _reset(self) -> None:
        """
        Subclass-specific reset logic. Override this in subclasses when
        additional state must be cleared on reset. Do not set ``self.status``
        here; ``reset()`` manages it.
        """
