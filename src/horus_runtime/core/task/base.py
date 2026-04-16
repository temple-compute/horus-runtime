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
Base Task. The task represents the unit of work that is executed by the Horus
runtime. The base task provides the foundational functionality for defining and
executing tasks, and should be ingested by the executor.
"""

from abc import abstractmethod
from asyncio import CancelledError
from typing import ClassVar, Self, final

from pydantic import Field, model_validator

from horus_builtin.event.task_event import HorusTaskEvent
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.executor.exceptions import IncompatibleRuntimeError
from horus_runtime.core.interaction.transport import BaseInteractionTransport
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.registry.auto_registry import AutoRegistry


class BaseTask(AutoRegistry, entry_point="task"):
    """
    The base task. This class provides the foundational functionality for
    defining and executing tasks, and should be ingested by the executor.
    """

    registry_key: ClassVar[str] = "kind"
    """
    The 'registry_key' field is used to identify the specific type of task.
    """

    kind: str
    """
    The 'kind' field is used to identify the specific type of task.
    """

    id: str
    """
    The task ID
    """

    name: str
    """
    Human-readable name for this task.
    """

    inputs: dict[str, BaseArtifact] = Field(default_factory=dict)
    """
    Input artifacts for this task. These are the artifacts that the task
    depends on.
    """

    outputs: dict[str, BaseArtifact] = Field(default_factory=dict)
    """
    Output artifacts for this task. These are the artifacts that the task
    produces.
    """

    executor: BaseExecutor
    """
    The executor that should execute this task. The executor is responsible for
    running the task in the appropriate environment (e.g., locally, on a remote
    server, in a container, etc.).
    """

    runtime: BaseRuntime
    """
    The runtime that should be used to execute this task. The runtime defines
    the actual command, program or script to run.
    """

    target: BaseTarget
    """
    The target that indicates where this task should be dispatched.
    """

    status: TaskStatus = TaskStatus.IDLE
    """
    The current status of the task's execution.
    """

    runs: int = 0
    """
    Number of times this task has been run. This can be used for tracking and
    debugging purposes.
    """

    skip_if_complete: bool = True
    """
    Whether to skip execution of this task if it is already complete.
    """

    interaction: BaseInteractionTransport | None = None
    """
    The interaction transport currently associated with the task run.
    """

    @model_validator(mode="after")
    def _validate_runtime_compatibility(self) -> Self:
        if not isinstance(self.runtime, self.executor.runtimes):
            raise IncompatibleRuntimeError(
                f"Runtime '{type(self.runtime).__name__}' is not compatible "
                f"with executor '{type(self.executor).__name__}'. "
                f"Expected one of: "
                f"{[r.__name__ for r in self.executor.runtimes]}"
            )
        return self

    @final
    async def run(self) -> None:
        """
        Execute the task, managing status transitions automatically.

        Subclasses must implement ``_run()`` instead of overriding this method.
        Status is driven entirely here:

        - ``RUNNING``   — set immediately on entry
        - ``COMPLETED`` — set on clean exit
        - ``CANCELED``  — set when ``CancelledError`` is raised
        - ``FAILED``    — set on any other exception (re-raised after)
        """
        ctx = HorusContext.get_context()
        if self.skip_if_complete and self.is_complete():
            ctx.bus.emit(
                HorusTaskEvent(
                    message=_("Skipping task %(task_name)s. Already complete.")
                    % {"task_name": self.name},
                    task_id=self.id,
                    task_name=self.name,
                )
            )
            return
        self.status = TaskStatus.RUNNING
        horus_logger.log.debug(
            _("Task %(task_name)s status → RUNNING") % {"task_name": self.name}
        )
        try:
            await self._run()
        except CancelledError:
            self.status = TaskStatus.CANCELED
            horus_logger.log.debug(
                _("Task %(task_name)s status → CANCELED")
                % {"task_name": self.name}
            )
            raise
        except Exception:
            self.status = TaskStatus.FAILED
            horus_logger.log.debug(
                _("Task %(task_name)s status → FAILED")
                % {"task_name": self.name}
            )
            raise
        else:
            self.status = TaskStatus.COMPLETED
            horus_logger.log.debug(
                _("Task %(task_name)s status → COMPLETED")
                % {"task_name": self.name}
            )

    async def sync_status(self) -> TaskStatus:
        """
        Refresh ``self.status`` from the target and return the updated value.

        For local in-process targets this is a no-op read. For remote targets
        (SSH, agent) this performs whatever async probe the target requires
        (HTTP poll, SSH check, etc.) and caches the result in ``self.status``
        so that synchronous callers can read it without I/O.
        """
        self.status = await self.target.get_status()
        return self.status

    @abstractmethod
    async def _run(self) -> None:
        """
        Task-specific execution logic. Implement this in subclasses.
        Do not set ``self.status`` here; ``run()`` manages it.
        """

    @abstractmethod
    def is_complete(self) -> bool:
        """
        Determine whether the task is complete by checking the existence and
        integrity of its output artifacts. This method should be implemented by
        subclasses to define the specific logic for determining task completion
        based on its outputs.
        """

    @final
    def reset(self) -> None:
        """
        Reset the task. This allows the task to be re-run from scratch.

        Resets ``self.status`` to ``IDLE`` and delegates subclass-specific
        reset logic to ``_reset()``.
        """
        self.status = TaskStatus.IDLE
        horus_logger.log.debug(
            _("Task %(task_name)s reset → IDLE") % {"task_name": self.name}
        )
        self._reset()

    @abstractmethod
    def _reset(self) -> None:
        """
        Subclass-specific reset logic. Override this in subclasses when
        additional state must be cleared on reset. Do not set ``self.status``
        here; ``reset()`` manages it.
        """
