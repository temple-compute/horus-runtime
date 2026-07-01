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
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Literal, NamedTuple, Protocol

from horus_runtime.settings import runtime_settings


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

        Unlike :meth:`communicate`, this returns data live, a line is
        yielded as soon as a newline is observed, not after the process
        exits. Don't call both on the same process: ``stream`` drains
        stdout/stderr as it goes, so a subsequent ``communicate`` would
        hang waiting on streams that are already empty.

        Exhausting the iterator means stdout/stderr have hit EOF; the
        process may not be *reaped* yet, so call :meth:`wait` afterward
        to get a reliable :attr:`returncode`.
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
