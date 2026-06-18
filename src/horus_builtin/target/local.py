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
Local target: executes tasks in the current process, running commands via
``asyncio.create_subprocess_shell`` with process-group isolation.

``LocalTarget`` implements the channel primitives by mapping
:data:`~horus_runtime.core.target.channel.RemotePath` to local
:class:`~pathlib.Path` objects — the only place in the codebase where that
mapping is allowed.  All other code must treat target-side paths as opaque
``RemotePath`` values and interact with the filesystem exclusively through
the channel methods.
"""

import asyncio
import os
import signal
import socket
from pathlib import Path
from typing import ClassVar

from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.target.channel import ChannelProcess, RemotePath


class _LocalChannelProcess(ChannelProcess):
    """
    ``ChannelProcess`` backed by an ``asyncio.subprocess.Process``.

    The subprocess is spawned with ``start_new_session=True`` so it leads
    its own process group.  :meth:`kill` terminates the entire group via
    ``os.killpg``, which prevents orphaned child processes when a command
    itself spawns children (the key correctness property for M1.2 / issue
    #66).
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    @property
    def returncode(self) -> int | None:
        """Exit code, or ``None`` if the process has not yet terminated."""
        return self._proc.returncode

    async def wait(self) -> int:
        """Wait for the process to finish and return its exit code."""
        return await self._proc.wait()

    async def communicate(self) -> tuple[bytes, bytes]:
        """
        Wait for the process to finish and return ``(stdout, stderr)`` bytes.
        """
        return await self._proc.communicate()

    def kill(self) -> None:
        """
        Kill the process group (SIGKILL), not just the process itself.

        Uses ``os.killpg`` so that any child processes spawned by the command
        are also terminated.  Safe to call even if the process has already
        exited (ignores ``ProcessLookupError``).
        """
        pid = self._proc.pid
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            # Process already exited; nothing to kill.
            pass

    def signal(self, sig: int) -> None:
        """Send *sig* to the process group."""
        pid = self._proc.pid
        try:
            os.killpg(os.getpgid(pid), sig)
        except ProcessLookupError:
            pass


class LocalTarget(BaseTarget):
    """
    Executes tasks directly in the current process.

    The dispatch lifecycle (``_dispatch``, ``wait``, ``cancel``,
    ``get_status``) is inherited from :class:`~.BaseTarget`.  This class
    only adds ``location_id``, ``access_cost``, and the channel primitives.
    """

    kind: str = "local"
    kind_name: ClassVar[str] = "Local"
    kind_description: ClassVar[str] = "Execute the task on the local machine."

    @property
    def location_id(self) -> str:
        """
        All ``LocalTarget`` instances on the same host share the local
        filesystem, so the hostname alone is a sufficient location key.
        """
        return f"local://{socket.gethostname()}"

    def access_cost(self, artifact: BaseArtifact) -> float | None:
        """
        Return ``0.0`` for artifacts that already exist on this machine and
        ``None`` otherwise.
        """
        return 0.0 if artifact.path.exists() else None

    # ------------------------------------------------------------------
    # Channel primitives (M1.2)
    #
    # RemotePath → Path mapping happens *only* here.  All paths received
    # as RemotePath are converted to local Path objects via Path(str(p)).
    # ------------------------------------------------------------------

    async def run_command(
        self,
        cmd: str,
        *,
        cwd: RemotePath | None = None,
        env: dict[str, str] | None = None,
    ) -> ChannelProcess:
        """
        Run *cmd* in a subprocess with process-group isolation.

        ``start_new_session=True`` gives the child its own process group
        (equivalent to ``setsid``).  :class:`_LocalChannelProcess.kill` then
        signals the whole group so that any grandchildren are also killed —
        the process-group isolation that issue #66 requires.

        Args:
            cmd: Shell command string.
            cwd: Working directory on the local filesystem (mapped from
                ``RemotePath`` to ``Path``).
            env: Extra variables merged onto ``os.environ``.

        Returns:
            A :class:`_LocalChannelProcess` wrapping the spawned process.
        """
        merged_env = {**os.environ, **(env or {})}
        local_cwd: Path | None = Path(str(cwd)) if cwd is not None else None

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            cwd=local_cwd,
            start_new_session=True,
        )
        return _LocalChannelProcess(proc)

    async def put_file(
        self,
        content: bytes | Path,
        remote_path: RemotePath,
    ) -> None:
        """
        Write *content* to *remote_path* on the local filesystem.

        Args:
            content: Raw bytes or a local ``Path`` whose contents are copied.
            remote_path: Destination path (mapped to a local ``Path``).
        """
        dest = Path(str(remote_path))
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (bytes, bytearray)):
            dest.write_bytes(content)
        else:
            dest.write_bytes(content.read_bytes())

    async def get_file(self, remote_path: RemotePath) -> bytes:
        """
        Read *remote_path* from the local filesystem and return its bytes.

        Args:
            remote_path: Source path (mapped to a local ``Path``).

        Returns:
            File contents as :class:`bytes`.
        """
        return Path(str(remote_path)).read_bytes()

    async def mkdir(self, path: RemotePath) -> None:
        """
        Create *path* (and all missing parents) on the local filesystem.

        Equivalent to ``mkdir -p``; idempotent.

        Args:
            path: Directory to create (mapped to a local ``Path``).
        """
        Path(str(path)).mkdir(parents=True, exist_ok=True)
