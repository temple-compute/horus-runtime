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
Channel primitives for agentless target communication.
"""

import asyncio
import contextlib
import shlex
import signal
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from itertools import zip_longest
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

from horus_runtime.settings import runtime_settings

if TYPE_CHECKING:
    from horus_runtime.core.target.base import BaseTarget


class RemoteDirEntry(NamedTuple):
    """
    One entry in a target directory listing.
    """

    name: str
    """Basename of the entry (the executor builds local paths from names)."""

    path: str
    """Absolute path of the entry on the **target** host."""

    is_dir: bool
    """Whether the entry is a directory."""

    size: int
    """File size in bytes; ``0`` for directories."""


StreamName = Literal["stdout", "stderr"]


class ChannelProcess(ABC):
    """
    Abstract handle for a command running on a target channel.
    """

    @property
    @abstractmethod
    def returncode(self) -> int | None:
        """
        Exit code of the process, or ``None`` if it has not yet terminated.
        """

    @abstractmethod
    async def wait(self) -> int:
        """
        Wait for the process to finish and return its exit code.

        Returns:
            The integer exit code.
        """

    @abstractmethod
    async def communicate(self) -> tuple[bytes, bytes]:
        """
        Wait for the process to finish, then return ``(stdout, stderr)`` as
        raw bytes. Callers decode as needed.

        Returns:
            A ``(stdout, stderr)`` tuple, both as :class:`bytes`.
        """

    @abstractmethod
    def kill(self) -> None:
        """
        Kill the process.
        """

    @abstractmethod
    def signal(self, sig: int) -> None:
        """
        Send *sig* to the process.

        Args:
            sig: A signal number (e.g. ``signal.SIGTERM``).
        """

    @abstractmethod
    def stream(self) -> AsyncGenerator[tuple[StreamName, bytes]]:
        """
        Yield ``(stream_name, line)`` pairs as the process produces them.
        """


class _LineReader(Protocol):
    async def readline(self) -> bytes: ...


async def merge_line_streams(
    stdout: _LineReader, stderr: _LineReader
) -> AsyncGenerator[tuple[StreamName, bytes]]:
    """
    Merge two line-oriented streams into a single async generator.
    """
    queue: asyncio.Queue[tuple[StreamName, bytes] | None | Exception] = (
        asyncio.Queue(maxsize=runtime_settings.STREAM_QUEUE_MAXSIZE)
    )

    # Pumping tasks read from the readers and put lines into the queue.  When
    # both readers are exhausted, a sentinel value is put into the queue to
    # signal the end of the stream.
    async def _pump(name: StreamName, reader: _LineReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            await queue.put((name, line))

    # Runner task waits for both pumps to finish and then puts a sentinel into
    # the queue to signal the end of the stream.
    async def _runner() -> None:
        try:
            await asyncio.gather(
                _pump("stdout", stdout), _pump("stderr", stderr)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(exc)
            return
        await queue.put(None)

    # Start the runner task and yield lines from the queue until the
    # sentinel is received.
    pump_task = asyncio.create_task(_runner())
    try:
        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task


@dataclass(frozen=True)
class JobHandle:
    """
    Opaque reference to a detached job launched by a target.
    """

    pid: int
    job_dir: str
    extra: dict[str, str] | None = None


def new_job_dir(base: str) -> str:
    """Return a unique per-launch marker directory under *base*."""
    return f"{base.rstrip('/')}/.horus_job/{uuid.uuid4().hex[:8]}"


def build_detach_command(
    inner: str, job_dir: str, *, session_leader: bool = False
) -> str:
    """
    Wrap *inner* (a shell command string, with ``cd``/``export`` already
    inlined by the caller) so it runs detached from the launching channel.

    The launching shell returns immediately after backgrounding the job.
    Works in POSIX ``sh`` (no ``disown`` needed), so it is identical for local
    and remote targets.
    """
    q = shlex.quote(job_dir)
    # Run the command in a subshell so a top-level `exit` in it can't skip the
    # exit-code write.
    body = f"( {inner} ); echo $? > {q}/exit_code"
    launcher = "setsid" if session_leader else "nohup"
    # mkdir runs in the foreground (`;`) so the marker dir exists before the
    # pid write; only the job is backgrounded (`&`), and `echo $!` captures
    # *its* pid (which is also its PGID under `setsid`).
    return (
        f"mkdir -p {q} || exit 1; "
        f"{launcher} sh -c {shlex.quote(body)} "
        f"> {q}/stdout.log 2> {q}/stderr.log < /dev/null & "
        f"echo $! > {q}/pid"
    )


@dataclass
class PollingChannelProcess(ChannelProcess):
    """
    :class:`ChannelProcess` for a detached job, driven by the launching
    target's primitives.
    """

    _target: "BaseTarget"
    _handle: JobHandle
    _returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        """Cached exit code; set once :meth:`wait` sees completion."""
        return self._returncode

    async def wait(self) -> int:
        """Poll until the job finishes; return its exit code."""
        while self._returncode is None:
            rc = await self._target.poll(self._handle)
            if rc is not None:
                self._returncode = rc
                break
            await asyncio.sleep(self._target.poll_interval)
        return self._returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        """Wait for completion, then read back captured stdout/stderr."""
        await self.wait()
        return await self._target.read_output(self._handle)

    def kill(self) -> None:
        """Best-effort SIGKILL to the detached job (fire-and-forget)."""
        self.signal(signal.SIGKILL)

    def signal(self, sig: int) -> None:
        """
        Send *sig* to the detached job without a held-open channel.

        Signal delivery may require a round trip (e.g. a short SSH exec), so it
        is scheduled on the running loop; callers confirm the effect via
        :meth:`wait` (which polls until ``exit_code`` appears).
        """
        asyncio.get_running_loop().create_task(
            self._target.send_signal(self._handle, sig)
        )

    async def stream(self) -> AsyncGenerator[tuple[StreamName, bytes]]:
        """
        Yield ``(stream_name, line)`` pairs by polling the captured log files.
        """
        offsets = {"stdout": 0, "stderr": 0}

        def emit(
            name: StreamName, data: bytes, *, final: bool
        ) -> list[tuple[StreamName, bytes]]:
            out: list[tuple[StreamName, bytes]] = []
            new = data[offsets[name] :]
            *lines, rest = new.split(b"\n")
            for line in lines:
                out.append((name, line + b"\n"))
            offsets[name] += len(new) - len(rest)
            if final and rest:
                out.append((name, rest))
                offsets[name] += len(rest)
            return out

        while True:
            done = self._returncode is not None
            if not done:
                rc = await self._target.poll(self._handle)
                if rc is not None:
                    self._returncode = rc
                    done = True
            out, err = await self._target.read_output(self._handle)
            # Read the log files in chunks, yielding lines as they appear.
            # If the job is done, yield any remaining partial lines too.
            stdout_items = emit("stdout", out, final=done)
            stderr_items = emit("stderr", err, final=done)
            for pair in zip_longest(stdout_items, stderr_items):
                for item in pair:
                    if item is not None:
                        yield item
            if done:
                return
            await asyncio.sleep(self._target.poll_interval)
