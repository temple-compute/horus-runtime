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

import asyncio
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, final

from pydantic import Field, PrivateAttr

from horus_runtime.core.target.channel import ChannelProcess, RemoteDirEntry
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.middleware.target import (
    TargetMiddleware,
    TargetMiddlewareContext,
)
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

    kind_name: ClassVar[str]
    """
    Human-friendly name of this kind of target.
    """

    kind_description: ClassVar[str]
    """
    Human-friendly description of this kind of target.
    """

    working_directory: str = Field(
        default_factory=lambda: Path.cwd().as_posix()
    )
    """
    Base directory on the target host where per-task working directories are
    created.
    """

    _task: "BaseTask | None" = PrivateAttr(default=None)
    _task_future: "asyncio.Task[None] | None" = PrivateAttr(default=None)

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

    @property
    def task_or_raise(self) -> "BaseTask":
        """
        Return the task currently running on this target, or raise an error if
        no task is running.
        """
        if self._task is None:
            raise TaskExecutionError(
                _("No task is currently running on this target.")
            )
        return self._task

    def bind(self, task: "BaseTask") -> None:
        """Associate *task* with this target ahead of dispatch.

        Resource-aware targets can then read ``task.resources`` during
        (possibly lazy) provisioning. Provisioning targets (e.g. Terraform)
        provision at transfer time, which happens before :meth:`dispatch` sets
        the task reference, so binding first gives them access to the task's
        declared resources in time.

        Args:
            task: The task about to be transferred to and dispatched on this
                target.

        Raises:
            TaskExecutionError: If a task is already running on this target.
        """
        if self._task_future is not None and not self._task_future.done():
            raise TaskExecutionError(
                _("Task '%(task_name)s' is already running on this target.")
                % {"task_name": self._task.name if self._task else "unknown"}
            )
        self._task = task

    @final
    async def dispatch(self, task: "BaseTask") -> None:
        """
        Start executing the given task on this target.
        """
        # Set the task to "PENDING" status before dispatching
        task.status = TaskStatus.PENDING
        await TargetMiddleware.call_with_middleware(
            TargetMiddlewareContext(
                target=self,
                task=task,
            ),
            lambda: self._dispatch(task),
        )

    async def _dispatch(self, task: "BaseTask") -> None:
        """
        Schedule *task* as an ``asyncio.Task`` in the current event loop.

        The task starts executing immediately; call :meth:`wait` to block
        until it finishes.  Override this in subclasses that need a different
        dispatch mechanism (e.g. submitting a remote job).

        Raises:
            TaskExecutionError: If the task is already running on this target.
        """
        self.bind(task)

        # Here _task is always set, so we can cast it
        self._task_future = asyncio.create_task(self.task_or_raise.run())
        horus_logger.log.debug(
            _("Dispatched task '%(task_name)s' to %(kind)s target")
            % {"task_name": task.name, "kind": self.kind}
        )

    async def wait(self) -> None:
        """
        Wait for the task to complete.

        Raises:
            TaskExecutionError: If the task has not been dispatched yet.
        """
        if self._task_future is None:
            raise TaskExecutionError(_("Task has not been dispatched yet."))

        horus_logger.log.debug(
            _(
                "Waiting for task '%(task_name)s' to complete"
                " on %(kind)s target"
            )
            % {
                "task_name": self._task.name if self._task else "unknown",
                "kind": self.kind,
            }
        )
        await self._task_future

    async def cancel(self) -> None:
        """
        Cancel the running task by injecting ``CancelledError`` at its next
        ``await`` point, then wait for it to finish any cleanup.
        """
        if self._task_future is None or self._task_future.done():
            return
        horus_logger.log.debug(
            _("Cancelling task '%(task_name)s' on %(kind)s target")
            % {
                "task_name": self._task.name if self._task else "unknown",
                "kind": self.kind,
            }
        )
        self._task_future.cancel()
        try:
            await self._task_future
        except asyncio.CancelledError:
            pass

    async def get_status(self) -> TaskStatus:
        """
        Get the current status of the task.

        Raises:
            TaskExecutionError: If no task has been dispatched yet.
        """
        if self._task_future is None:
            raise TaskExecutionError(_("Task has not been dispatched yet."))
        return self.task_or_raise.status

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
        the artifact metadata such as ``artifact.path`` and ``artifact.kind``
        (or the concrete artifact type) for kind-specific cost adjustments.
        """

    async def recover(self) -> bool:
        """
        Attempt to reconnect to a previously dispatched task after
        orchestrator restart. Returns True if recovery succeeded.
        By default, recovery is not supported and this method returns False.
        """
        return False

    def path_on_target(self, artifact: "BaseArtifact") -> str:
        """
        Absolute path where *artifact* lives on **this target's** filesystem.

        Lets runtimes reference artifacts without the caller hand-building
        remote paths: a command like ``python {script}`` resolves to the right
        location on whichever target the task runs on. The default assumes the
        artifact is reachable at its own path (same filesystem as the
        orchestrator); targets that copy artifacts elsewhere (e.g. SSH)
        override this to point at the on-host copy.
        """
        return str(artifact.path)

    @abstractmethod
    async def run_command(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ChannelProcess:
        """
        Run *cmd* on the target and return a :class:`.ChannelProcess` handle.

        Args:
            cmd: Shell command string to execute.
            cwd: Working directory on the *target* host.  The channel
                applies this — ``LocalTarget`` passes it as
                ``subprocess cwd=``; remote targets inline
                ``cd <cwd> && …`` before the command.
            env: Additional environment variables to merge onto the
                channel's base environment.  Keys/values are plain strings.

        Returns:
            A :class:`.ChannelProcess` handle for the running command.
        """

    @abstractmethod
    async def put_file(
        self,
        content: bytes | Path,
        remote_path: str,
    ) -> None:
        """
        Write *content* to *remote_path* on the target.

        Args:
            content: Either raw :class:`bytes` or a local
                :class:`~pathlib.Path` whose contents are read and sent.
            remote_path: Destination path on the *target* host.
        """

    @abstractmethod
    async def get_file(self, remote_path: str) -> bytes:
        """
        Read *remote_path* from the target and return its contents as bytes.

        Args:
            remote_path: Path on the *target* host to read.

        Returns:
            The file contents as :class:`bytes`.  Callers decode as needed.
        """

    @abstractmethod
    async def mkdir(self, path: str) -> None:
        """
        Create *path* (and all missing parents) on the target.

        Semantics are equivalent to ``mkdir -p``; no error is raised if the
        directory already exists.

        Args:
            path: Directory path to create on the *target* host.
        """

    @abstractmethod
    async def list_dir(self, path: str) -> list[RemoteDirEntry]:
        """
        List the immediate children of *path* on the target host
        (non-recursive).

        Implementations must use a **native, non-shell** mechanism so this is
        OS-agnostic (``pathlib`` locally, SFTP/agent API remotely) and must
        **skip symlinks** (they cause cycles and are almost always noise in a
        side-artifacts directory). Each :class:`.RemoteDirEntry` carries the
        file ``size`` (``0`` for directories) so callers can enforce a size cap
        before transferring.

        Args:
            path: Directory path on the *target* host to list.

        Returns:
            One :class:`.RemoteDirEntry` per top-level child, or an empty list
            if *path* does not exist or is not a directory.
        """
