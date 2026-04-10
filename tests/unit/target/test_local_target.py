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
import socket
from unittest.mock import Mock

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.function import FunctionTask
from horus_runtime.context import HorusContext
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

    def test_access_cost_zero_for_bare_path(self) -> None:
        """
        A bare filesystem path (no scheme) has zero access cost.
        """
        target = LocalTarget()
        artifact = FileArtifact(uri="/tmp/some_file.txt")
        assert target.access_cost(artifact) == 0.0

    def test_access_cost_zero_for_file_scheme(self) -> None:
        """
        A ``file://`` URI has zero access cost.
        """
        target = LocalTarget()
        artifact = FileArtifact(uri="file:///tmp/some_file.txt")
        assert target.access_cost(artifact) == 0.0

    def test_access_cost_none_for_remote_scheme(self) -> None:
        """
        A remote URI (e.g. ``s3://``) returns None, signalling transfer needed.
        """
        target = LocalTarget()
        artifact = Mock(uri="s3://bucket/key")
        assert target.access_cost(artifact) is None


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
            name="slow_task",
            inputs={},
            outputs={},
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
