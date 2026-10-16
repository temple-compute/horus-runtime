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
Unit tests for the LocalTarget builtin target.
"""

import asyncio
import os
import socket
import tempfile
from contextlib import aclosing
from pathlib import Path
from unittest.mock import Mock

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.function import FunctionTask
from horus_runtime.context import HorusContext
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.core.task.status import TaskStatus
from tests.conftest import MakeTaskType


@pytest.mark.unit
class TestLocalTargetProperties:
    """
    Tests for LocalTarget configuration and metadata.
    """

    def test_kind_is_local(self) -> None:
        """
        The 'kind' field must be 'local'.
        """
        assert LocalTarget().kind == "local"

    def test_location_id_uses_local_scheme_and_hostname(self) -> None:
        """
        location_id must follow the ``local://<hostname>`` format.
        """
        target = LocalTarget()
        assert target.location_id == f"local://{socket.gethostname()}"

    def test_access_cost_zero_for_existing_local_path(self) -> None:
        """
        An existing local filesystem path has zero access cost.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "some_file.txt"
            path.write_text("content")

            target = LocalTarget()
            artifact = FileArtifact(id="test_artifact", path=path)
            assert target.access_cost(artifact) == 0.0

    def test_access_cost_none_for_missing_local_path(self) -> None:
        """
        A missing local path returns None, signalling transfer needed.
        """
        target = LocalTarget()
        artifact = Mock(path=Path("/definitely/nonexistent/file.txt"))
        assert target.access_cost(artifact) is None

    def test_resolved_working_directory_falls_back_to_cwd(self) -> None:
        """
        With no working_directory set, LocalTarget resolves to the process
        CWD (the historical default) instead of raising.
        """
        target = LocalTarget()
        assert target.working_directory is None
        assert target.resolved_working_directory == Path.cwd().as_posix()

    def test_resolved_working_directory_uses_explicit_value(self) -> None:
        """
        An explicit working_directory is returned as-is, not overridden by the
        CWD fallback.
        """
        target = LocalTarget(working_directory="/explicit/dir")
        assert target.resolved_working_directory == "/explicit/dir"

    def test_resolved_working_directory_absolutizes_relative_value(
        self,
    ) -> None:
        """
        A relative working_directory (e.g. propagated from a relative
        orchestrator target) is resolved to an absolute path against the
        process CWD. working_dir is used both as the subprocess cwd and as an
        in-command path prefix, so a relative value would double the path.
        """
        target = LocalTarget(working_directory="horus_workflow_results")
        resolved = target.resolved_working_directory
        assert Path(resolved).is_absolute()
        assert resolved == (Path.cwd() / "horus_workflow_results").as_posix()


@pytest.mark.unit
class TestLocalTargetDispatch:
    """
    Tests for LocalTarget task lifecycle: dispatch / wait / cancel / status.
    """

    async def test_wait_raises_before_dispatch(self) -> None:
        """
        Calling wait() before dispatch raises TaskExecutionError.
        """
        target = LocalTarget()
        with pytest.raises(TaskExecutionError):
            await target.wait()

    async def test_get_status_raises_before_dispatch(self) -> None:
        """
        Calling get_status() before dispatch raises TaskExecutionError.
        """
        target = LocalTarget()
        with pytest.raises(TaskExecutionError):
            await target.get_status()

    async def test_cancel_before_dispatch_is_noop(self) -> None:
        """
        cancel() before dispatch does not raise.
        """
        target = LocalTarget()
        await target.cancel()  # should not raise

    async def test_dispatch_and_wait_complete_successfully(
        self, make_shell_task: MakeTaskType, horus_context: HorusContext
    ) -> None:
        """
        A dispatched task runs to completion; wait() returns without error.
        """
        # Fixture is required to set up the runtime context
        # but not used directly in this test body
        del horus_context

        target = LocalTarget()
        task = make_shell_task()
        task.target = target

        await target.dispatch(task)
        await target.wait()

        assert task.status == TaskStatus.COMPLETED

    async def test_get_status_reflects_task_status(
        self, make_shell_task: MakeTaskType, horus_context: HorusContext
    ) -> None:
        """
        get_status() reads the status directly from the dispatched task.
        """
        # Fixture is required to set up the runtime context
        # but not used directly in this test body
        del horus_context

        target = LocalTarget()
        task = make_shell_task()
        task.target = target

        await target.dispatch(task)
        await target.wait()

        status = await target.get_status()
        assert status == TaskStatus.COMPLETED

    async def test_cancel_stops_running_task(
        self, horus_context: HorusContext
    ) -> None:
        """
        cancel() cancels a still-running task and does not raise.
        """
        del horus_context  # Fixture is required to set up the runtime context

        async def run_slow_task() -> None:
            # Simulate a long-running task by sleeping for a while
            await asyncio.sleep(10)

        task = FunctionTask(
            id="slow_task",
            name="slow_task",
            inputs=[],
            outputs=[],
            runtime=PythonFunctionRuntime(func=run_slow_task),
        )

        # Dispatch the task to the LocalTarget
        await task.target.dispatch(task)

        # Yield to the event loop so task.run() can start and reach its first
        # await point before we cancel it.
        await asyncio.sleep(0)

        # Now cancel the task while it's still running
        await task.target.cancel()

        assert task.status == TaskStatus.CANCELED


@pytest.mark.unit
class TestLocalTargetListDir:
    """
    Tests for LocalTarget.list_dir.
    """

    async def test_lists_files_and_dirs_with_sizes(
        self, tmp_path: Path
    ) -> None:
        """
        Files report their size; directories report 0 and is_dir=True.
        """
        (tmp_path / "a.txt").write_text("hello")  # 5 bytes
        (tmp_path / "sub").mkdir()

        target = LocalTarget()
        entries = {
            e.name: e for e in await target.list_dir(tmp_path.as_posix())
        }

        assert set(entries) == {"a.txt", "sub"}
        assert entries["a.txt"].is_dir is False
        assert entries["a.txt"].size == 5
        assert entries["a.txt"].path == (tmp_path / "a.txt").as_posix()
        assert entries["sub"].is_dir is True
        assert entries["sub"].size == 0

    async def test_skips_symlinks(self, tmp_path: Path) -> None:
        """
        Symlinks are not listed (cycle/noise protection).
        """
        (tmp_path / "real.txt").write_text("x")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")

        target = LocalTarget()
        names = {e.name for e in await target.list_dir(tmp_path.as_posix())}

        assert names == {"real.txt"}

    async def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        """
        Listing a non-existent path returns an empty list.
        """
        target = LocalTarget()
        assert await target.list_dir((tmp_path / "nope").as_posix()) == []


@pytest.mark.unit
class TestLocalTargetFileCopy:
    """
    Tests for LocalTarget.put_file and get_file_to with paths.
    """

    async def test_put_file_from_path(self, tmp_path: Path) -> None:
        """
        A Path source is copied to the destination, creating parents.
        """
        src = tmp_path / "src.bin"
        src.write_bytes(b"data")
        dest = tmp_path / "a" / "b" / "dest.bin"

        await LocalTarget().put_file(src, dest.as_posix())

        assert dest.read_bytes() == b"data"

    async def test_get_file_to_copies_file(self, tmp_path: Path) -> None:
        """
        get_file_to copies the remote path into the local file.
        """
        src = tmp_path / "src.bin"
        src.write_bytes(b"data")
        local = tmp_path / "local.bin"

        await LocalTarget().get_file_to(src.as_posix(), local)

        assert local.read_bytes() == b"data"


@pytest.mark.unit
class TestLocalTargetStream:
    """
    Tests for ChannelProcess.stream() on LocalChannelProcess — live
    (stream_name, line) delivery instead of batch communicate().
    """

    async def test_stream_yields_stdout_lines(self, tmp_path: Path) -> None:
        """stream() yields lines from stdout as they are produced."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("printf 'one\\ntwo\\nthree\\n'")

        lines = [line async for _stream, line in proc.stream()]

        assert lines == [b"one\n", b"two\n", b"three\n"]
        assert await proc.wait() == 0

    async def test_stream_yields_stderr_lines(self, tmp_path: Path) -> None:
        """stream() yields lines from stderr as they are produced."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("printf 'err1\\nerr2\\n' >&2")

        results = [(s, line) async for s, line in proc.stream()]

        assert results == [("stderr", b"err1\n"), ("stderr", b"err2\n")]

    async def test_stream_labels_stream_names_correctly(
        self, tmp_path: Path
    ) -> None:
        """stream() labels lines with the correct stream name."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("echo out_line; echo err_line >&2")

        by_stream: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        async for stream_name, line in proc.stream():
            by_stream[stream_name].append(line)

        assert by_stream["stdout"] == [b"out_line\n"]
        assert by_stream["stderr"] == [b"err_line\n"]

    async def test_stream_empty_output_yields_nothing(
        self, tmp_path: Path
    ) -> None:
        """A command that produces no output yields no lines from stream()."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("true")

        lines = [line async for _stream, line in proc.stream()]

        assert lines == []
        assert await proc.wait() == 0

    async def test_stream_exhaustion_then_wait_returns_real_exit_code(
        self, tmp_path: Path
    ) -> None:
        """After exhausting stream(), wait() returns the real exit code."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("echo done; exit 7")

        async for _stream, _line in proc.stream():
            pass

        assert await proc.wait() == 7

    async def test_stream_delivers_lines_before_process_exits(
        self, tmp_path: Path
    ) -> None:
        """
        Lines must be observable while the process is still running, not
        only after it exits — this is the whole point of stream() vs
        communicate().
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("echo first; sleep 0.2; echo second")

        gen = proc.stream()
        stream_name, line = await asyncio.wait_for(gen.__anext__(), timeout=2)

        assert (stream_name, line) == ("stdout", b"first\n")
        # Confirm we observed it while the process is still alive/sleeping.
        assert proc.returncode is None

        await gen.aclose()
        proc.kill()
        await proc.wait()

    async def test_stream_can_be_stopped_early_with_aclosing(
        self, tmp_path: Path
    ) -> None:
        """
        Breaking out of consumption via contextlib.aclosing cleans up the
        underlying pump tasks deterministically.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command(
            "for i in $(seq 1 100); do echo $i; sleep 0.05; done"
        )

        seen = []
        async with aclosing(proc.stream()) as lines:
            async for _stream, line in lines:
                seen.append(line)
                if len(seen) == 3:
                    break

        assert seen == [b"1\n", b"2\n", b"3\n"]

        proc.kill()
        await proc.wait()

    async def test_stream_allows_kill_mid_stream(self, tmp_path: Path) -> None:
        """
        A consumer can call kill() while iterating stream() — e.g. on
        detecting a fatal stderr line — and the iterator ends cleanly.
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("echo bad >&2; sleep 1")

        async with aclosing(proc.stream()) as lines:
            async for stream_name, line in lines:
                if stream_name == "stderr" and b"bad" in line:
                    proc.kill()
                    break

        code = await proc.wait()
        assert code != 0

    async def test_stream_does_not_deadlock_on_large_output(
        self, tmp_path: Path
    ) -> None:
        """
        Output larger than a pipe buffer must not deadlock when consumed
        via stream() (regression guard vs. naive wait()-then-read).
        """
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command(
            "for i in $(seq 1 5000); do echo line_$i; done"
        )

        count = 0
        async for _stream, _line in proc.stream():
            count += 1

        assert count == 5000
        assert await proc.wait() == 0


_MIB = 1024 * 1024


async def _read(
    target: LocalTarget, path: Path, offset: int, length: int, *, base: bool
) -> list[bytes]:
    """Collect read_range chunks from the LocalTarget or the dd default."""
    if base:
        it = BaseTarget.read_range(target, path.as_posix(), offset, length)
    else:
        it = target.read_range(path.as_posix(), offset, length)
    return [chunk async for chunk in it]


@pytest.mark.unit
@pytest.mark.parametrize("base", [True, False], ids=["dd", "local"])
class TestReadRange:
    """
    Tests for the dd default of BaseTarget.read_range and the LocalTarget
    override.
    """

    async def test_ranges(self, tmp_path: Path, base: bool) -> None:
        """First, middle and last ranges match the file slices."""
        data = os.urandom(3 * _MIB + 123)
        src = tmp_path / "f.bin"
        src.write_bytes(data)
        target = LocalTarget()

        for offset, length in [(0, _MIB), (_MIB, _MIB + 5), (3 * _MIB, 123)]:
            got = b"".join(await _read(target, src, offset, length, base=base))
            assert got == data[offset : offset + length]

    async def test_short_file_raises(self, tmp_path: Path, base: bool) -> None:
        """A file that ends before the range raises."""
        src = tmp_path / "f.bin"
        src.write_bytes(b"x" * (_MIB + 10))

        for offset in (0, 2 * _MIB):
            with pytest.raises(TaskExecutionError):
                await _read(LocalTarget(), src, offset, _MIB + 11, base=base)

    async def test_unaligned_offset(self, tmp_path: Path, base: bool) -> None:
        """An offset that is not a whole MiB is rejected."""
        src = tmp_path / "f.bin"
        src.write_bytes(b"x")

        with pytest.raises(ValueError):
            await _read(LocalTarget(), src, 1, 1, base=base)


@pytest.mark.unit
class TestReadRangeDefault:
    """Tests specific to the dd default of BaseTarget.read_range."""

    async def test_dd_failure_raises(self, tmp_path: Path) -> None:
        """A non-zero dd exit raises TaskExecutionError."""
        with pytest.raises(TaskExecutionError):
            await _read(LocalTarget(), tmp_path / "missing", 0, 1, base=True)


@pytest.mark.unit
class TestLocalReadRangeChunks:
    """Tests specific to LocalTarget.read_range."""

    async def test_chunks_at_most_8_mib(self, tmp_path: Path) -> None:
        """A large range is yielded in chunks of at most 8 MiB."""
        data = os.urandom(20 * _MIB)
        src = tmp_path / "f.bin"
        src.write_bytes(data)

        chunks = await _read(LocalTarget(), src, _MIB, 18 * _MIB, base=False)

        assert len(chunks) == 3
        assert max(len(c) for c in chunks) <= 8 * _MIB
        assert b"".join(chunks) == data[_MIB : 19 * _MIB]
