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
Unit tests for detachable execution: the ``run_command`` template method,
the shared ``_PollingChannelProcess``, and the ``LocalTarget`` primitives.

These exercise the real detach path (nohup'd subprocess + marker files) since
``LocalTarget`` runs on the local filesystem.
"""

import asyncio
import os
import signal
from contextlib import aclosing
from pathlib import Path

import pytest

from horus_builtin.target.local import LocalChannelProcess, LocalTarget
from horus_runtime.core.target.channel import (
    _PollingChannelProcess,
    build_detach_command,
    new_job_dir,
)


@pytest.mark.unit
class TestDetachDefaults:
    """The default detach behavior is target-specific."""

    def test_local_defaults_to_sync(self) -> None:
        """LocalTarget keeps the synchronous path by default."""
        assert LocalTarget.detach_by_default is False

    async def test_local_sync_path_returns_local_channel_process(
        self, tmp_path: Path
    ) -> None:
        """Default (non-detached) LocalTarget keeps its live-streaming path."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("echo hi")
        assert isinstance(proc, LocalChannelProcess)
        assert await proc.wait() == 0

    async def test_explicit_detach_returns_polling_process(
        self, tmp_path: Path
    ) -> None:
        """Explicit detach=True yields a polling process."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("true", detach=True)
        assert isinstance(proc, _PollingChannelProcess)
        assert await proc.wait() == 0


@pytest.mark.unit
class TestLocalDetachedExecution:
    """End-to-end detached execution on LocalTarget."""

    async def test_captures_output_and_exit_code(self, tmp_path: Path) -> None:
        """Detached run captures stdout/stderr and a zero exit code."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command(
            "echo out; echo err 1>&2", cwd=tmp_path.as_posix(), detach=True
        )
        out, err = await proc.communicate()
        assert proc.returncode == 0
        assert out == b"out\n"
        assert err == b"err\n"

    async def test_nonzero_exit_code_is_recorded(self, tmp_path: Path) -> None:
        """A non-zero exit code survives the exit_code marker round trip."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("exit 3", detach=True)
        assert await proc.wait() == 3

    async def test_env_and_cwd_are_applied(self, tmp_path: Path) -> None:
        """Env vars and cwd are inlined into the detached command."""
        workdir = tmp_path / "work"
        workdir.mkdir()
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command(
            'echo "$GREETING in $(basename "$PWD")"',
            cwd=workdir.as_posix(),
            env={"GREETING": "hello"},
            detach=True,
        )
        out, _ = await proc.communicate()
        assert out == b"hello in work\n"

    async def test_stream_yields_lines(self, tmp_path: Path) -> None:
        """stream() yields captured stdout/stderr lines with labels."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command(
            "echo a; echo b; echo c 1>&2", detach=True
        )
        seen: list[tuple[str, bytes]] = []
        async with aclosing(proc.stream()) as stream:
            async for name, line in stream:
                seen.append((name, line))
        assert (b"a\n") in [line for _, line in seen]
        assert ("stderr", b"c\n") in seen
        # Draining the stream hits EOF; wait() still reports the exit code.
        assert await proc.wait() == 0

    async def test_signal_terminates_detached_job(
        self, tmp_path: Path
    ) -> None:
        """signal() reaches the detached job and ends it."""
        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("sleep 30", detach=True)
        # Give the nohup'd job a moment to start, then kill it.
        await asyncio.sleep(0.3)
        proc.signal(signal.SIGKILL)
        rc = await asyncio.wait_for(proc.wait(), timeout=5)
        assert rc != 0

    async def test_signal_kills_child_processes(self, tmp_path: Path) -> None:
        """
        signal() targets the whole process group, so a child the command
        spawned is stopped too (not left orphaned).
        """
        marker = tmp_path / "child.pid"
        target = LocalTarget(working_directory=tmp_path.as_posix())
        # Spawn a background grandchild that records its pid, then both sleep.
        proc = await target.run_command(
            f"sleep 30 & echo $! > {marker.as_posix()}; wait",
            detach=True,
        )

        for _ in range(50):  # wait for the child to record its pid
            if marker.exists() and marker.read_text().strip():
                break
            await asyncio.sleep(0.1)
        child_pid = int(marker.read_text().strip())
        assert os.getpgid(child_pid)  # child is alive

        proc.signal(signal.SIGKILL)
        await asyncio.wait_for(proc.wait(), timeout=5)

        for _ in range(50):  # child should die with the group
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)


@pytest.mark.unit
class TestDetachHelpers:
    """The shared wrapper/marker-dir helpers."""

    def test_new_job_dir_is_unique_and_under_base(self) -> None:
        """Each call returns a distinct marker dir under the base."""
        a = new_job_dir("/tmp/x")
        b = new_job_dir("/tmp/x")
        assert a != b
        assert a.startswith("/tmp/x/.horus_job/")

    def test_wrapper_records_pid_and_exit_code(self) -> None:
        """The wrapper backgrounds the job and records pid/exit_code/logs."""
        wrapper = build_detach_command("mycmd", "/jobs/1")
        assert "nohup" in wrapper
        assert "/jobs/1/pid" in wrapper
        assert "/jobs/1/exit_code" in wrapper
        assert "/jobs/1/stdout.log" in wrapper

    def test_wrapper_session_leader_uses_setsid(self) -> None:
        """session_leader launches under setsid for group signalling."""
        wrapper = build_detach_command("mycmd", "/jobs/1", session_leader=True)
        assert "setsid" in wrapper
        assert "nohup" not in wrapper
