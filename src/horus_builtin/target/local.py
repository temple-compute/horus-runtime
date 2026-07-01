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
"""

import asyncio
import os
import shlex
import signal
import socket
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import ClassVar

from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.target.channel import (
    ChannelProcess,
    JobHandle,
    RemoteDirEntry,
    StreamName,
    build_detach_command,
    merge_line_streams,
)


class LocalChannelProcess(ChannelProcess):
    """
    ``ChannelProcess`` backed by an ``asyncio.subprocess.Process``.

    The subprocess is spawned with ``start_new_session=True`` so it leads
    its own process group.  :meth:`kill` terminates the entire group via
    ``os.killpg``, which prevents orphaned child processes when a command
    itself spawns children.
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
        Kill the process group (SIGKILL).

        Uses ``os.killpg`` so that any child processes spawned by the command
        are also terminated.  Safe to call even if the process has already
        exited.
        """
        self.signal(signal.SIGKILL)

    def signal(self, sig: int) -> None:
        """Send *sig* to the process group."""
        pid = self._proc.pid

        kill_func = os.killpg if os.name == "posix" else os.kill
        parsed_pid = os.getpgid(pid) if os.name == "posix" else pid

        try:
            kill_func(parsed_pid, sig)
        except ProcessLookupError:
            pass

    def stream(self) -> AsyncGenerator[tuple[StreamName, bytes]]:
        """
        Yield ``(stream_name, line)`` pairs as the process produces them.
        """
        if self._proc.stdout is None or self._proc.stderr is None:
            raise RuntimeError("stdout and stderr must be piped (use PIPE)")
        return merge_line_streams(self._proc.stdout, self._proc.stderr)


class LocalTarget(BaseTarget):
    """
    Executes tasks directly in the current process.
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

    # No droppable channel locally, so keep the live-streaming path as the
    # default; detachment is still available (e.g. for recovery) via the
    # primitives below.
    detach_by_default: ClassVar[bool] = False

    # Local jobs live on the same filesystem, so polling is cheap; keep it
    # snappy for near-live streaming when detachment is requested.
    poll_interval: ClassVar[float] = 0.25

    async def _run_command_sync(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ChannelProcess:
        """
        Run *cmd* in a subprocess with process-group isolation.

        Args:
            cmd: Shell command string.
            cwd: Working directory on the local filesystem
            env: Extra variables merged onto ``os.environ``.

        Returns:
            A :class:`LocalChannelProcess` wrapping the spawned process.
        """
        # Windows does not support process groups, so we cannot use
        # start_new_session=True.  Instead, we rely on the default behavior
        # of subprocesses on Windows to terminate child processes when the
        # parent process exits. (windows os.name == "nt")
        start_new_session = True if os.name == "posix" else False

        merged_env = {**os.environ, **(env or {})}
        local_cwd: Path | None = Path(str(cwd)) if cwd is not None else None

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            cwd=local_cwd,
            start_new_session=start_new_session,
        )
        return LocalChannelProcess(proc)

    async def _launch(
        self,
        cmd: str,
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        job_dir: str,
    ) -> JobHandle:
        """
        Launch *cmd* detached: nohup'd, redirected to log files under
        *job_dir*, surviving the orchestrator process.
        """
        exports = "".join(
            f"export {k}={shlex.quote(v)}; " for k, v in (env or {}).items()
        )
        inner = f"{exports}{cmd}"
        if cwd is not None:
            inner = f"cd {shlex.quote(cwd)} && {inner}"
        wrapper = build_detach_command(inner, job_dir)

        # The launcher shell backgrounds the job and returns immediately; the
        # nohup'd job reparents and keeps running independent of this process.
        launcher = await asyncio.create_subprocess_shell(
            wrapper,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
        await launcher.wait()
        pid = int((Path(job_dir) / "pid").read_text().strip())
        return JobHandle(pid=pid, job_dir=job_dir)

    async def _poll(self, handle: JobHandle) -> int | None:
        """``None`` while running; the recorded exit code once finished."""
        ec = Path(handle.job_dir) / "exit_code"
        if ec.exists():
            txt = ec.read_text().strip()
            if txt:
                return int(txt)
        try:
            os.kill(handle.pid, 0)
        except ProcessLookupError:
            # Gone without an exit_code: killed or lost.
            return -1
        except PermissionError:
            pass
        return None

    async def _read_output(self, handle: JobHandle) -> tuple[bytes, bytes]:
        """Read the captured stdout/stderr log files."""

        def _read(name: str) -> bytes:
            p = Path(handle.job_dir) / name
            return p.read_bytes() if p.exists() else b""

        return _read("stdout.log"), _read("stderr.log")

    async def _send_signal(self, handle: JobHandle, sig: int) -> None:
        """Best-effort signal to the detached job."""
        # ponytail: signals the recorded pid only; a forked tree may survive.
        try:
            os.kill(handle.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    async def put_file(
        self,
        content: bytes | Path,
        remote_path: str,
    ) -> None:
        """
        Write *content* to *remote_path* on the local filesystem.

        Args:
            content: Raw bytes or a local ``Path`` whose contents are copied.
            remote_path: Destination path (mapped to a local ``Path``).
        """
        dest = Path(remote_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (bytes, bytearray)):
            dest.write_bytes(content)
        else:
            dest.write_bytes(content.read_bytes())

    async def get_file(self, remote_path: str) -> bytes:
        """
        Read *remote_path* from the local filesystem and return its bytes.

        Args:
            remote_path: Source path (mapped to a local ``Path``).

        Returns:
            File contents as :class:`bytes`.
        """
        return Path(remote_path).read_bytes()

    async def mkdir(self, path: str) -> None:
        """
        Create *path* (and all missing parents) on the local filesystem.

        Equivalent to ``mkdir -p``; idempotent.

        Args:
            path: Directory to create (mapped to a local ``Path``).
        """
        Path(path).mkdir(parents=True, exist_ok=True)

    async def list_dir(self, path: str) -> list[RemoteDirEntry]:
        """
        List the immediate children of *path* on the local filesystem.

        Args:
            path: Directory to list (mapped to a local ``Path``).

        Returns:
            One :class:`RemoteDirEntry` per child, or ``[]`` if *path* is not a
            directory.
        """
        base = Path(path)
        if not base.is_dir():
            return []

        entries: list[RemoteDirEntry] = []
        for child in base.iterdir():
            if child.is_symlink():
                continue
            is_dir = child.is_dir()
            size = 0 if is_dir else child.stat().st_size
            entries.append(
                RemoteDirEntry(
                    name=child.name,
                    path=child.as_posix(),
                    is_dir=is_dir,
                    size=size,
                )
            )
        return entries
