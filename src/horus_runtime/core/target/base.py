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
import shlex
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, final

from pydantic import PrivateAttr

from horus_runtime.core.target.channel import (
    ChannelProcess,
    JobHandle,
    PollingChannelProcess,
    RemoteDirEntry,
    new_job_dir,
)
from horus_runtime.core.target.exceptions import WorkingDirectoryNotSetError
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.middleware.target import (
    TargetMiddleware,
    TargetMiddlewareContext,
)
from horus_runtime.middleware.target_command import (
    TargetCommandMiddleware,
    TargetCommandMiddlewareContext,
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

    working_directory: str | None = None
    """
    Base directory on the target host where per-task working directories are
    created. Left ``None`` until resolved: the workflow fills it in for targets
    co-located with the orchestrator (see
    :meth:`BaseWorkflow._propagate_orchestrator_working_directory`); every
    other target decides for itself what an unset value means via
    :attr:`resolved_working_directory`.
    """

    _task: "BaseTask | None" = PrivateAttr(default=None)
    _task_future: "asyncio.Task[None] | None" = PrivateAttr(default=None)

    @property
    def resolved_working_directory(self) -> str:
        """
        The base working directory as a concrete path, resolved at use time.

        ``working_directory`` may be ``None``. The base contract requires it to
        have been set (explicitly or by orchestrator propagation) and raises
        otherwise; targets that can derive a sensible default when it is unset
        (e.g. the local machine's current directory) override this property.

        Raises:
            WorkingDirectoryNotSetError: When ``working_directory`` is ``None``
                and the target does not derive one.
        """
        if self.working_directory is None:
            raise WorkingDirectoryNotSetError(self.kind)
        return self.working_directory

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
        """Associate *task* with this target ahead of dispatch, so resource-
        aware targets can read ``task.resources`` while provisioning.

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

        A relative, dimensionless value used to compare sources:

        - ``0.0``   — zero-cost local read (same filesystem, in-memory, …)
        - ``> 0.0`` — accessible but non-free (network, agent API, …)
        - ``None``  — not accessible; transfer required before dispatch

        Must be synchronous and cheap (no I/O).
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

        The default assumes the artifact is reachable at its own path (same
        filesystem as the orchestrator); targets that copy artifacts elsewhere
        (e.g. SSH) override this to point at the on-host copy.
        """
        return str(artifact.path)

    poll_interval: ClassVar[float] = 1.0
    """
    Seconds between status polls for detached jobs.
    """

    detach_by_default: ClassVar[bool] = True
    """
    Whether :meth:`run_command` detaches when the caller doesn't specify.
    """

    async def run_command(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        detach: bool | None = None,
    ) -> ChannelProcess:
        """
        Run *cmd* on the target and return a :class:`.ChannelProcess` handle.

        The command is routed through the ``TargetCommandMiddleware`` chain
        first, so middleware may rewrite it (e.g. wrap it in an instrumentation
        tool) before it is dispatched.
        """
        ctx = TargetCommandMiddlewareContext(
            target=self,
            command=cmd,
            cwd=cwd,
            env=env,
            detach=detach,
        )
        return await TargetCommandMiddleware.call_with_middleware(
            ctx,
            lambda: self._run_command(ctx),
        )

    async def _run_command(
        self, ctx: TargetCommandMiddlewareContext
    ) -> ChannelProcess:
        """
        Dispatch the (possibly rewritten) command from *ctx* to the target.
        """
        detach = ctx.detach
        if detach is None:
            detach = self.detach_by_default
        if not detach:
            return await self.run_command_sync(
                ctx.command, cwd=ctx.cwd, env=ctx.env
            )
        job_dir = new_job_dir(ctx.cwd or self.resolved_working_directory)
        handle = await self.launch(
            ctx.command, cwd=ctx.cwd, env=ctx.env, job_dir=job_dir
        )
        return PollingChannelProcess(self, handle)

    @abstractmethod
    async def run_command_sync(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ChannelProcess:
        """Run *cmd* synchronously over a live channel (``detach=False``)."""

    @abstractmethod
    async def launch(
        self,
        cmd: str,
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        job_dir: str,
    ) -> JobHandle:
        """Start *cmd* detached from the launching channel; return a handle."""

    @abstractmethod
    async def poll(self, handle: JobHandle) -> int | None:
        """Non-blocking status: ``None`` while running, exit code once done."""

    @abstractmethod
    async def read_output(self, handle: JobHandle) -> tuple[bytes, bytes]:
        """Return the job's captured ``(stdout, stderr)`` so far."""

    @abstractmethod
    async def send_signal(self, handle: JobHandle, sig: int) -> None:
        """Best-effort signal delivery to a detached job (no live channel)."""

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

        Implementations must use a native, non-shell mechanism (``pathlib``
        locally, SFTP/agent API remotely) and skip symlinks.

        Args:
            path: Directory path on the *target* host to list.

        Returns:
            One :class:`.RemoteDirEntry` per top-level child, or an empty list
            if *path* does not exist or is not a directory.
        """

    async def path_exists(self, path: str) -> bool:
        """
        Whether *path* exists on the target host (file or directory).

        The default probes over a shell channel; targets that can answer
        natively (local filesystem, SFTP, agent API) should override this.
        """
        out = await self.run_command_sync(f"test -e {shlex.quote(path)}")
        return await out.wait() == 0

    async def remove(self, path: str) -> None:
        """
        Remove *path* (file or directory, recursively) on the target host.

        Idempotent: removing a missing path is not an error. The default runs
        over a shell channel; targets that can answer natively should override.
        """
        out = await self.run_command_sync(f"rm -rf {shlex.quote(path)}")
        await out.wait()
