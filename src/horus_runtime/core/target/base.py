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
The Horus target indicates where a task should be dispatched and executed.
"""

from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar, final

from horus_runtime.core.task.status import TaskStatus
from horus_runtime.registry.auto_registry import AutoRegistry

if TYPE_CHECKING:
    from horus_runtime.core.artifact.base import BaseArtifact
    from horus_runtime.core.task.base import BaseTask


class BaseTarget(AutoRegistry, entry_point="target"):
    """
    Base class for task targets. A target describes *where* a task runs
    (local, remote agent, provisioned cloud machine, etc.).
    """

    registry_key: ClassVar[str] = "kind"
    kind: str

    @property
    @abstractmethod
    def location_id(self) -> str:
        """
        Stable identifier for the physical location this target runs on.
        Two targets with the same ``location_id`` share a filesystem, so no
        artifact transfer is needed between them.

        Implementations should return a URI-like string that is:
        - deterministic across process restarts on the same host/node
        - unique across distinct machines or agent instances

        Examples:
            ``local://hostname``
            ``ssh://user@gpu-box``
            ``horus-agent://agent-42``
        """

    @final
    async def dispatch(self, task: "BaseTask") -> None:
        """
        Start executing the given task on this target.
        """
        # Set the task to "PENDING" status before dispatching
        task.status = TaskStatus.PENDING

        await self._dispatch(task)

    @abstractmethod
    async def _dispatch(self, task: "BaseTask") -> None:
        """
        Internal dispatch method to be implemented by subclasses. This is
        called by the public dispatch() method, which handles common logic like
        retries and error handling.
        """

    @abstractmethod
    async def wait(self) -> None:
        """
        Wait for the task to complete. Raises ``TaskExecutionError`` if the
        task fails during execution.
        """

    @abstractmethod
    async def cancel(self) -> None:
        """
        Cancel the task if it is still running. Raises ``TaskExecutionError``
        if the task fails to cancel.
        """

    @abstractmethod
    async def get_status(self) -> "TaskStatus":
        """
        Get the current status of the task.
        """

    @abstractmethod
    def access_cost(self, artifact: "BaseArtifact") -> float | None:
        """
        Return the estimated cost of reading ``artifact`` from this target,
        or ``None`` if this target cannot access it at all.

        The value is dimensionless and relative, callers use it to compare
        sources and decide whether a transfer is preferable:

        - ``0.0``   — zero-cost local read (same filesystem, in-memory, …)
        - ``> 0.0`` — accessible but non-free (network, agent API, …)
        - ``None``  — not accessible; transfer required before dispatch

        Implementations must be synchronous and cheap (no I/O). Use
        ``artifact.uri`` for location/protocol checks and ``artifact.kind``
        (or the concrete artifact type) for kind-specific cost adjustments.
        """

    async def recover(self) -> bool:
        """
        Attempt to reconnect to a previously dispatched task after
        orchestrator restart. Returns True if recovery succeeded.
        By default, recovery is not supported and this method returns False.
        """
        return False
