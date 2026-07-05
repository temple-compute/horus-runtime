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
from collections.abc import Awaitable
from pathlib import Path
from typing import ClassVar, NamedTuple, Self, final
from uuid import UUID, uuid4

import yaml
from pydantic import Field, PrivateAttr, model_validator

from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.transfer.exceptions import (
    OrchestratorTargetNotSetError,
    TransferStrategyNotFoundError,
)
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

    _base_directory: Path | None = PrivateAttr(default=None)
    """
    Directory relative paths are anchored to (the run directory and, through
    it, artifact paths and logs). Set to the workflow YAML's folder by
    :meth:`from_yaml`; ``None`` for programmatically-built workflows, which
    fall back to the process CWD. Runtime-only state, not serialized.
    """

    @model_validator(mode="after")
    def check_unique_task_ids(self) -> Self:
        """
        Validates that all tasks have unique ids. This is required for correct
        dependency resolution and execution.
        """
        seen_ids = set()
        for task in self.tasks:
            if task.id in seen_ids:
                raise TaskIdsAreNotUniqueError(task.id)
            seen_ids.add(task.id)
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

        def _check_unique_artifact_ids(artifacts: list[BaseArtifact]) -> None:
            """
            Helper function to check for unique artifact ids within a list of
            artifacts. Raises an error if duplicates are found.
            """
            seen_ids: set[str] = set()
            for artifact in artifacts:
                if artifact.id in seen_ids:
                    raise ArtifactIdsAreNotUniqueError(artifact.id)
                seen_ids.add(artifact.id)

        # Root artifacts must have unique ids across the workflow.
        _check_unique_artifact_ids(self.artifacts)

        # Output and input artifacts must have unique ids within each task.
        for task in self.tasks:
            _check_unique_artifact_ids(task.outputs)
            _check_unique_artifact_ids(task.inputs)

        return self

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
        - be the only edge feeding its ``(target, target_input)``.
        """
        task_inputs = {t.id: {a.id for a in t.inputs} for t in self.tasks}
        task_outputs = {t.id: {a.id for a in t.outputs} for t in self.tasks}
        root_ids = {a.id for a in self.artifacts}

        seen_targets: set[tuple[str, str]] = set()
        for edge in self.edges:
            # Target must resolve to a real task input.
            if edge.target not in task_inputs:
                raise UnknownEdgeEndpointError("target task", edge.target)
            if edge.target_input not in task_inputs[edge.target]:
                raise UnknownEdgeEndpointError(
                    "target input", edge.target_input
                )

            # At most one edge may feed a given consumer input.
            key = (edge.target, edge.target_input)
            if key in seen_targets:
                raise DuplicateEdgeTargetError(edge.target, edge.target_input)
            seen_targets.add(key)

            # Source must resolve to a task output or a root artifact.
            if edge.source in task_outputs:
                if edge.source_output not in task_outputs[edge.source]:
                    raise UnknownEdgeEndpointError(
                        "source output", edge.source_output
                    )
            elif edge.source.startswith("artifact-"):
                if edge.source_output not in root_ids:
                    raise UnknownEdgeEndpointError(
                        "root artifact", edge.source_output
                    )
            else:
                raise UnknownEdgeEndpointError("source task", edge.source)

        return self

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
        workflow._base_directory = Path(path).resolve().parent
        return workflow

    def to_yaml(self, path: str | Path) -> None:
        """
        Save the workflow to a YAML file.

        Args:
            path: Path to the YAML file.

        Returns:
            None
        """
        with Path(path).open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self.model_dump(), fh)

    def _build_source_map(self) -> dict[tuple[str, str], _EdgeSource]:
        """
        Resolve, for the whole workflow, where each consumer input is sourced.

        Returns a map keyed by (target task id, input id) to an
        :class:`_EdgeSource`. Edges are validated at construction
        (see :meth:`check_edges_resolve`), so every entry resolves to a real
        producer output or root artifact.
        """
        targets_by_task = {t.id: t.target for t in self.tasks}
        outputs_by_task = {
            (t.id, o.id): o for t in self.tasks for o in t.outputs
        }
        roots_by_id = {a.id: a for a in self.artifacts}

        source_map: dict[tuple[str, str], _EdgeSource] = {}
        for edge in self.edges:
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
            TransferStrategyNotFoundError: When no registered strategy handles
                the resolved source → destination target pair.
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

            # Look up the registered strategy for this (source, dest) pair.
            strategy_cls = BaseTransferStrategy.get_from_registry(
                source_target, task.target
            )
            if strategy_cls is None:
                raise TransferStrategyNotFoundError(
                    source_target.kind, task.target.kind
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
                    "strategy": strategy_cls.__name__,
                }
            )

            # Perform the transfer, then point the consumer input at the
            # materialized location so its body templating resolves correctly.
            await strategy_cls().transfer(
                transfer_art, source_target, task.target
            )
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

    @final
    def _resolve_run_paths(self) -> Path:
        """
        Anchor this run's relative paths to the single :attr:`run_directory`.

        - Point the orchestrator target (and, by propagation, co-located task
          targets) at the absolute run directory, so per-task working dirs
          nest under it.
        - Make declared artifact paths absolute: a path produced by some task
          (a task output, or an input fed by an upstream output) resolves
          under the run directory; a path never produced (an external input)
          resolves under the base directory. Paths declared absolute are left
          untouched.

        Only relative declared paths are rewritten, so calling this twice is
        safe. Returns the resolved run root so callers can use it directly.
        """
        base = self._effective_base
        run_root = self.run_directory

        if self.orchestrator_target is not None:
            self.orchestrator_target.working_directory = run_root.as_posix()

        produced = {
            artifact._declared_path
            for task in self.tasks
            for artifact in task.outputs
            if artifact._declared_path is not None
        }

        def anchor(artifact: BaseArtifact) -> None:
            declared = artifact._declared_path
            if declared is None or declared.is_absolute():
                return
            root = run_root if declared in produced else base
            artifact.path = (root / declared).resolve()

        for task in self.tasks:
            for artifact in (*task.inputs, *task.outputs):
                anchor(artifact)
            # A runtime's local source file (e.g. a python_script's ``script``)
            # is provided relative to the workflow file, so anchor it to the
            # base dir too. Accessed by name to avoid a core->builtin import.
            script = getattr(task.runtime, "script", None)
            if isinstance(script, Path) and not script.is_absolute():
                task.runtime.script = (base / script).resolve()
        for artifact in self.artifacts:
            anchor(artifact)

        return run_root

    @final
    def _propagate_orchestrator_working_directory(self) -> None:
        """
        Give every co-located task target the orchestrator target's working
        directory as its base, so local tasks run inside the orchestrator's
        folder (each still nested under its own ``working_dir / task.id``).

        A target is co-located with the orchestrator when it shares its
        ``location_id`` (same filesystem). A task target that already has a
        ``working_directory`` set (not ``None``) is left untouched.
        """
        if self.orchestrator_target is None:
            return

        base = self.orchestrator_target.working_directory
        if base is None:
            return

        orchestrator_loc = self.orchestrator_target.location_id
        for task in self.tasks:
            target = task.target
            if target.working_directory is not None:
                continue
            if target.location_id == orchestrator_loc:
                target.working_directory = base

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
    def reset(self) -> None:
        """
        Reset the workflow by deleting all output artifacts of all tasks in the
        workflow. This allows the workflow to be re-run from scratch.
        """
        self.status = WorkflowStatus.IDLE
        horus_logger.log.debug(
            _("Resetting workflow %(workflow_name)s.")
            % {"workflow_name": self.name}
        )
        self._reset()

    @abstractmethod
    def _reset(self) -> None:
        """
        Subclass-specific reset logic. Override this in subclasses when
        additional state must be cleared on reset. Do not set ``self.status``
        here; ``reset()`` manages it.
        """
