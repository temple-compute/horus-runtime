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

The workflow executes tasks in the order they are defined. It does not
currently perform dependency resolution; ordering is the author's
responsibility when writing the workflow YAML file.
"""

from abc import abstractmethod
from asyncio import CancelledError
from pathlib import Path
from typing import ClassVar, Self, final

from pydantic import Field, model_validator

from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.transfer.exceptions import (
    OrchestratorTargetNotSetError,
    TransferStrategyNotFoundError,
)
from horus_runtime.core.transfer.strategy import BaseTransferStrategy
from horus_runtime.core.workflow.status import WorkflowStatus
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.registry.auto_registry import AutoRegistry


class BaseWorkflow(AutoRegistry, entry_point="workflow"):
    """
    Orchestrates an ordered collection of tasks.
    """

    registry_key: ClassVar[str] = "kind"

    kind: str
    """
    The 'kind' field is used to identify the specific type of workflow.
    """

    name: str
    """
    Human-readable name for this workflow.
    """

    tasks: dict[str, BaseTask] = Field(
        default_factory=dict,
    )
    """
    Ordered mapping of task names to task instances.
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

    @model_validator(mode="after")
    def inject_task_ids(self) -> Self:
        """
        After workflow initialization, inject task IDs to each task.
        """
        for tid, task in self.tasks.items():
            task.task_id = tid

        return self

    @classmethod
    @abstractmethod
    def from_yaml(cls, path: str | Path) -> Self:
        """
        Load a workflow from a YAML file.

        Args:
            path: Path to the YAML file.

        Returns:
            A fully constructed :class:`BaseWorkflow` instance.
        """

    async def transfer_artifacts(self, task: BaseTask) -> None:
        """
        Transfer the input artifacts of the given task to the target where the
        task will run, using the appropriate transfer strategies.

        Called by the workflow before dispatching a task to ensure all inputs
        are available on the task's target. If the destination target can
        already access an artifact (``access_cost`` returns a non-None value),
        no transfer is performed for that artifact.

        The source target for each artifact is resolved as follows:

        1. If the artifact URI matches an output of a previously defined task,
           that task's target is used as the source.
        2. Otherwise the artifact is treated as a root input (user-provided)
           and ``self.orchestrator_target`` is used as the source. If
           ``orchestrator_target`` is ``None`` a
           :exc:`OrchestratorTargetNotSetError` is raised.

        Args:
            task: The task whose input artifacts should be transferred.

        Raises:
            OrchestratorTargetNotSetError: When a root artifact cannot be
                accessed by the destination and no orchestrator_target is set.
            TransferStrategyNotFoundError: When no registered strategy handles
                the resolved source → destination target pair.
        """
        # Build a reverse map: artifact URI → the target of the task that
        # produced it. This covers outputs of every task in the workflow,
        # not just the ones that have already run.
        uri_to_source: dict[str, BaseTarget] = {}
        for t in self.tasks.values():
            for artifact in t.outputs.values():
                uri_to_source[artifact.uri] = t.target

        for artifact in task.inputs.values():
            # Skip artifacts the destination can already access.
            if task.target.access_cost(artifact) is not None:
                horus_logger.log.debug(
                    _(
                        "Artifact '%(uri)s' is accessible by target"
                        " '%(kind)s'; skipping transfer."
                    )
                    % {"uri": artifact.uri, "kind": task.target.kind}
                )
                continue

            # Resolve the source target.
            source_target = uri_to_source.get(artifact.uri)
            if source_target is None:
                # Root artifact: must come from the orchestrator.
                if self.orchestrator_target is None:
                    raise OrchestratorTargetNotSetError(
                        artifact.uri, task.target.kind
                    )
                source_target = self.orchestrator_target

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
                    "Transferring artifact '%(uri)s' from '%(src)s'"
                    " to '%(dst)s' via %(strategy)s."
                )
                % {
                    "uri": artifact.uri,
                    "src": source_target.kind,
                    "dst": task.target.kind,
                    "strategy": type(strategy_cls).__name__,
                }
            )

            # Perform the transfer.
            await strategy_cls().transfer(artifact, source_target, task.target)

    @final
    async def run(self) -> None:
        """
        Execute the workflow, managing status transitions automatically.

        Subclasses must implement ``_run()`` instead of overriding this method.
        Status is driven entirely here:

        - ``RUNNING``   — set immediately on entry
        - ``COMPLETED`` — set on clean exit
        - ``CANCELED``  — set when ``CancelledError`` is raised
        - ``FAILED``    — set on any other exception (re-raised after)
        """
        self.status = WorkflowStatus.RUNNING
        horus_logger.log.debug(
            _("Workflow %(workflow_name)s status → RUNNING")
            % {"workflow_name": self.name}
        )
        try:
            await self._run()
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

    @abstractmethod
    async def _run(self) -> None:
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
