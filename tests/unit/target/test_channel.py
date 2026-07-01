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
Unit tests for channel primitives: ChannelProcess ABC, and LocalTarget channel
implementation.
"""

import asyncio
import os
import signal
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from pathlib import Path

import pytest

from horus_builtin.target.local import LocalChannelProcess, LocalTarget
from horus_runtime.core.target.channel import (
    ChannelProcess,
    StreamName,
    merge_line_streams,
)


@pytest.mark.unit
class TestChannelProcessABC:
    """
    Tests for the ChannelProcess abstract base class.
    """

    def test_channel_process_is_abstract(self) -> None:
        """
        ChannelProcess cannot be instantiated directly.
        """
        with pytest.raises(TypeError):
            ChannelProcess()  # type: ignore[abstract]

    def test_concrete_implementation_works(self) -> None:
        """
        A fully-implemented subclass can be instantiated.
        """

        class _Concrete(ChannelProcess):
            @property
            def returncode(self) -> int | None:
                return 0

            async def wait(self) -> int:
                return 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

            def kill(self) -> None:
                pass

            def signal(self, sig: int) -> None:
                pass

            async def stream(self) -> AsyncGenerator[tuple[StreamName, bytes]]:
                return
                yield

        proc = _Concrete()
        assert proc.returncode == 0

    def test_concrete_implementation_missing_stream_raises(self) -> None:
        """
        A subclass implementing every primitive except stream() is still
        abstract and cannot be instantiated.
        """

        class _MissingStream(ChannelProcess):
            @property
            def returncode(self) -> int | None:
                return 0

            async def wait(self) -> int:
                return 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

            def kill(self) -> None:
                pass

            def signal(self, sig: int) -> None:
                pass

        with pytest.raises(TypeError):
            _MissingStream()  # type: ignore[abstract]

    async def test_concrete_stream_is_async_iterable(self) -> None:
        """
        stream() on a concrete implementation can be consumed via
        ``async for``.
        """

        class _Concrete(ChannelProcess):
            @property
            def returncode(self) -> int | None:
                return 0

            async def wait(self) -> int:
                return 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

            def kill(self) -> None:
                pass

            def signal(self, sig: int) -> None:
                pass

            async def stream(self) -> AsyncGenerator[tuple[StreamName, bytes]]:
                yield ("stdout", b"hello\n")

        proc = _Concrete()
        items = [item async for item in proc.stream()]
        assert items == [("stdout", b"hello\n")]


@pytest.mark.unit
class TestLocalTargetRunCommand:
    """
    Tests for LocalTarget.run_command — the channel's main primitive.
    """

    async def test_run_command_returns_channel_process(
        self, tmp_path: Path
    ) -> None:
        """
        run_command returns a ChannelProcess instance.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("echo hello")
        stdout, _stderr = await proc.communicate()
        assert isinstance(proc, ChannelProcess)
        assert stdout.strip() == b"hello"
        assert proc.returncode == 0

    async def test_run_command_captures_stdout_as_bytes(
        self, tmp_path: Path
    ) -> None:
        """
        communicate() returns (stdout, stderr) as raw bytes.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("echo 'hello world'")
        stdout, stderr = await proc.communicate()
        assert b"hello world" in stdout
        assert stderr == b""

    async def test_run_command_captures_stderr_as_bytes(
        self, tmp_path: Path
    ) -> None:
        """
        Stderr bytes are available via communicate().
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("echo err >&2")
        _stdout, stderr = await proc.communicate()
        assert b"err" in stderr

    async def test_run_command_returncode_zero_on_success(
        self, tmp_path: Path
    ) -> None:
        """
        Return code is 0 for a successful command.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("true")
        await proc.wait()
        assert proc.returncode == 0

    async def test_run_command_returncode_nonzero_on_failure(
        self, tmp_path: Path
    ) -> None:
        """
        Return code is non-zero for a failing command.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("false")
        await proc.wait()
        assert proc.returncode != 0

    async def test_run_command_env_is_merged(self, tmp_path: Path) -> None:
        """
        Extra env vars from the ``env`` kwarg are present in the subprocess.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command(
            "echo $MY_TEST_VAR",
            env={"MY_TEST_VAR": "channel_works"},
        )
        stdout, _ = await proc.communicate()
        assert b"channel_works" in stdout

    async def test_run_command_cwd_is_applied(self, tmp_path: Path) -> None:
        """
        The ``cwd`` kwarg sets the working directory for the subprocess.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command(
            "pwd",
            cwd=tmp_path.as_posix(),
        )
        stdout, _ = await proc.communicate()
        # tmp_path may contain symlinks; resolve both sides.
        assert Path(stdout.strip().decode()).resolve() == tmp_path.resolve()

    async def test_run_command_wait_returns_exit_code(
        self, tmp_path: Path
    ) -> None:
        """
        wait() returns the integer exit code.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command(
            "exit 42",
        )
        code = await proc.wait()
        assert code == 42

    async def test_run_command_signal_sends_to_process(
        self, tmp_path: Path
    ) -> None:
        """
        signal() delivers the given signal to the process group.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("sleep 30")
        proc.signal(signal.SIGTERM)
        code = await proc.wait()
        # SIGTERM → exit code -15 on most UNIX systems
        # (or 143 if the shell catches it)
        assert code != 0


@pytest.mark.unit
class TestLocalTargetGroupKill:
    """
    Group-kill acceptance test (M1.2 / issue #66).

    A command that spawns a child process must leave no orphan after kill().
    """

    @pytest.mark.skipif(
        sys.platform == "win32", reason="process groups are POSIX-only"
    )
    async def test_kill_terminates_whole_process_group(
        self, tmp_path: Path
    ) -> None:
        """
        kill() must terminate the child process that the command spawns.

        Spawn ``sh -c 'sleep 60 & echo $!'``, capture the grandchild PID,
        call kill(), then assert the grandchild is gone.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())

        # The grandchild PID is printed to stdout so we can probe it later.
        proc = await target.run_command(
            "sleep 60 & CHILD_PID=$!; echo $CHILD_PID; wait $CHILD_PID"
        )

        assert isinstance(proc, LocalChannelProcess)

        # Read the child PID from the first line of stdout (non-blocking:
        # communicate() blocks until the process finishes, so instead we
        # read from the pipe via proc._proc.stdout).
        assert proc._proc.stdout is not None
        first_line = await proc._proc.stdout.readline()
        child_pid = int(first_line.strip())

        # Confirm the grandchild is alive before kill().
        try:
            os.kill(child_pid, 0)  # signal 0 = probe; raises if gone
        except ProcessLookupError:
            pytest.fail("grandchild process was not alive before kill()")

        proc.kill()
        await proc.wait()

        # Give the OS a brief moment to reap the grandchild.
        deadline = time.monotonic() + 3.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)

        assert not alive, (
            f"Grandchild PID {child_pid} is still alive after group kill"
        )


@pytest.mark.unit
class TestLocalTargetFileOps:
    """
    Tests for LocalTarget.put_file / get_file / mkdir.
    """

    async def test_put_and_get_bytes_round_trip(self, tmp_path: Path) -> None:
        """
        put_file(bytes) then get_file returns the original bytes.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        dest = (tmp_path / "test_file.bin").as_posix()
        content = b"\x00\x01\x02hello"

        await target.put_file(content, dest)
        result = await target.get_file(dest)

        assert result == content

    async def test_put_path_and_get_bytes_round_trip(
        self, tmp_path: Path
    ) -> None:
        """
        put_file(Path) copies the local file; get_file returns its bytes.
        """
        src = tmp_path / "src.txt"
        src.write_bytes(b"from path")

        target = LocalTarget(working_directory=tmp_path.as_posix())
        dest = (tmp_path / "subdir" / "dst.txt").as_posix()

        await target.put_file(src, dest)
        result = await target.get_file(dest)

        assert result == b"from path"

    async def test_put_file_creates_parent_dirs(self, tmp_path: Path) -> None:
        """
        put_file creates missing parent directories automatically.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        dest = (tmp_path / "a" / "b" / "c" / "file.txt").as_posix()

        await target.put_file(b"nested", dest)

        assert Path(dest).exists()

    async def test_mkdir_creates_directory(self, tmp_path: Path) -> None:
        """
        The mkdir method creates the directory on the local filesystem.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        new_dir = (tmp_path / "new_dir").as_posix()

        await target.mkdir(new_dir)

        assert Path(new_dir).is_dir()

    async def test_mkdir_is_idempotent(self, tmp_path: Path) -> None:
        """
        The mkdir method does not raise when the directory exists (mkdir -p).
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        existing = (tmp_path / "already_there").as_posix()
        Path(existing).mkdir()

        await target.mkdir(existing)  # must not raise

    async def test_mkdir_creates_nested_dirs(self, tmp_path: Path) -> None:
        """
        The mkdir method creates all intermediate parents (mkdir -p semantics).
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        deep = (tmp_path / "x" / "y" / "z").as_posix()

        await target.mkdir(deep)

        assert Path(deep).is_dir()


@pytest.mark.unit
class TestMergeLineStreams:
    """
    Tests for channel.merge_line_streams — the stdout/stderr interleaving
    helper shared by every ChannelProcess.stream() implementation.
    """

    class _FakeReader:
        """A minimal stand-in for anything with an async readline()."""

        def __init__(self, lines: list[bytes], delay: float = 0.0) -> None:
            self._lines = list(lines)
            self._delay = delay

        async def readline(self) -> bytes:
            if self._delay:
                await asyncio.sleep(self._delay)
            if not self._lines:
                return b""
            return self._lines.pop(0)

    async def test_yields_all_lines_from_both_streams(self) -> None:
        """Lines from both stdout and stderr must be yielded."""
        stdout = self._FakeReader([b"out1\n", b"out2\n"])
        stderr = self._FakeReader([b"err1\n"])

        results = [item async for item in merge_line_streams(stdout, stderr)]

        assert ("stdout", b"out1\n") in results
        assert ("stdout", b"out2\n") in results
        assert ("stderr", b"err1\n") in results
        assert len(results) == 3

    async def test_ends_when_both_readers_hit_eof(self) -> None:
        """
        The generator must terminate when both readers return EOF.
        """
        stdout = self._FakeReader([])
        stderr = self._FakeReader([])

        results = [item async for item in merge_line_streams(stdout, stderr)]

        assert results == []

    async def test_preserves_within_stream_order(self) -> None:
        """
        Lines from the *same* stream must come out in the order produced,
        even though stdout/stderr interleave with each other.
        """
        stdout = self._FakeReader([b"1\n", b"2\n", b"3\n"])
        stderr = self._FakeReader([])

        results = [
            line async for _stream, line in merge_line_streams(stdout, stderr)
        ]

        assert results == [b"1\n", b"2\n", b"3\n"]

    async def test_early_break_does_not_raise_on_cleanup(self) -> None:
        """
        Breaking out of consumption mid-stream must not leave the pump
        tasks dangling or raise when the generator is closed.
        """
        stdout = self._FakeReader([b"a\n"], delay=0.01)
        stderr = self._FakeReader([b"b\n", b"c\n", b"d\n"], delay=0.01)

        gen = merge_line_streams(stdout, stderr)
        async for _stream, _line in gen:
            break

        await gen.aclose()  # must not raise

    async def test_aclosing_stops_iteration_cleanly(self) -> None:
        """Using contextlib.aclosing() must stop iteration cleanly."""
        stdout = self._FakeReader([b"a\n", b"b\n", b"c\n"], delay=0.01)
        stderr = self._FakeReader([])

        seen = []
        async with aclosing(merge_line_streams(stdout, stderr)) as gen:
            async for _stream, line in gen:
                seen.append(line)
                if len(seen) == 1:
                    break

        assert seen == [b"a\n"]

    async def test_pump_exception_propagates_not_hangs(self) -> None:
        """
        If a reader's readline() raises, the consumer must see that
        exception (eventually) rather than hang on queue.get() forever.
        """

        class _FailingReader:
            async def readline(self) -> bytes:
                raise ConnectionResetError("boom")

        stdout = self._FakeReader([])
        stderr = _FailingReader()

        with pytest.raises(ConnectionResetError):
            async for _ in merge_line_streams(stdout, stderr):
                pass

    async def test_pump_stop_async_iteration_gets_wrapped_by_pep479(
        self,
    ) -> None:
        """
        A reader that raises StopAsyncIteration is an edge case: Python's
        generator protocol (PEP 479) forbids a bare StopAsyncIteration from
        escaping merge_line_streams' frame, so it surfaces as RuntimeError
        with the original exception chained as __cause__. This documents
        that behavior rather than treating it as a bug — no real reader
        should ever raise StopAsyncIteration from readline() in practice.
        """

        class _FailingReader:
            async def readline(self) -> bytes:
                raise StopAsyncIteration("boom")

        stdout = self._FakeReader([])
        stderr = _FailingReader()

        with pytest.raises(RuntimeError) as exc_info:
            async for _ in merge_line_streams(stdout, stderr):
                pass

        assert isinstance(exc_info.value.__cause__, StopAsyncIteration)
