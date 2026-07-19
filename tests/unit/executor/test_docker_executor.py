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
Unit tests for DockerExecutor (Bug #72: cancel stops container).

These tests mock all subprocess calls so no real Docker daemon is required.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horus_builtin.executor.docker import DockerExecutor
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.target.local import LocalTarget
from horus_runtime.core.executor.base import BaseExecutor


@pytest.mark.unit
class TestDockerExecutorContainerTracking:
    """
    Verify that DockerExecutor tracks container IDs and that
    stop_running_container calls ``docker stop``.
    """

    def test_docker_executor_is_base_executor_subclass(self) -> None:
        """DockerExecutor must inherit from BaseExecutor."""
        assert issubclass(DockerExecutor, BaseExecutor)

    def test_docker_executor_kind(self) -> None:
        """DockerExecutor.kind must be 'docker'."""
        exec_ = DockerExecutor(image="python:3.12-slim")
        assert exec_.kind == "docker"

    def test_container_id_starts_as_none(self) -> None:
        """_container_id is None before any task has run."""
        exec_ = DockerExecutor(image="python:3.12-slim")
        assert exec_._container_id is None

    async def test_stop_running_container_noop_when_no_container(
        self,
    ) -> None:
        """
        stop_running_container is a no-op when no container is running
        (_container_id is None).
        """
        exec_ = DockerExecutor(image="python:3.12-slim")
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await exec_.stop_running_container()
        mock_exec.assert_not_called()

    async def test_stop_running_container_calls_docker_stop(self) -> None:
        """
        stop_running_container calls ``docker stop <container_id>`` when
        a container ID is tracked.
        """
        exec_ = DockerExecutor(image="python:3.12-slim")
        exec_._container_id = "abc123def456"

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch(
            "horus_builtin.executor.docker.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            await exec_.stop_running_container()

        mock_exec.assert_called_once_with(
            "docker",
            "stop",
            "abc123def456",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        mock_proc.wait.assert_called_once()

    async def test_stop_running_container_clears_container_id(self) -> None:
        """
        stop_running_container sets _container_id back to None after stopping,
        so repeated calls are idempotent.
        """
        exec_ = DockerExecutor(image="python:3.12-slim")
        exec_._container_id = "abc123def456"

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch(
            "horus_builtin.executor.docker.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            await exec_.stop_running_container()

        assert exec_._container_id is None

    async def test_stop_running_container_idempotent(self) -> None:
        """
        Calling stop_running_container twice only issues one docker stop.
        """
        exec_ = DockerExecutor(image="python:3.12-slim")
        exec_._container_id = "abc123def456"

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch(
            "horus_builtin.executor.docker.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            await exec_.stop_running_container()
            await exec_.stop_running_container()  # second call: no container

        assert mock_exec.call_count == 1


@pytest.mark.unit
class TestCancelStopsContainer:
    """
    Verify that BaseTarget.cancel() calls executor.stop_running_container()
    before cancelling the asyncio task (Bug #72).
    """

    async def test_cancel_invokes_stop_running_container(
        self, tmp_path: "pytest.TempPathFactory"
    ) -> None:
        """
        When cancel() is called on a target whose task has an executor,
        stop_running_container must be called before the asyncio task is
        cancelled.

        We use an AsyncMock for the executor so we can track the call without
        fighting Pydantic's attribute restrictions.
        """
        target = LocalTarget(working_directory=str(tmp_path))

        # Use an AsyncMock so we can verify stop_running_container is called.
        mock_executor = AsyncMock(spec=DockerExecutor)
        mock_executor.stop_running_container = AsyncMock()

        mock_task = MagicMock()
        mock_task.executor = mock_executor
        # Pydantic V2 private attrs: use object.__setattr__
        object.__setattr__(target, "_task", mock_task)

        # Plant a still-running future so cancel() doesn't short-circuit.
        loop = asyncio.get_event_loop()
        never_done: asyncio.Future[None] = loop.create_future()
        object.__setattr__(target, "_task_future", never_done)

        # cancel() must not raise even though the future never resolves on
        # its own (CancelledError is swallowed internally).
        await target.cancel()

        mock_executor.stop_running_container.assert_called_once()
        never_done.cancel()  # clean up the dangling future

    async def test_cancel_without_task_does_not_raise(self) -> None:
        """
        cancel() is safe to call when _task is None (e.g. before dispatch).
        """
        target = LocalTarget()
        # _task_future is None -> should return immediately without error
        await target.cancel()

    async def test_base_executor_stop_running_container_is_noop(
        self,
    ) -> None:
        """
        The base class stop_running_container is a no-op (no docker call).
        The ShellExecutor inherits this and must not raise.
        """
        executor = ShellExecutor()
        # Must complete without error and without calling any subprocess.
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await executor.stop_running_container()
        mock_exec.assert_not_called()
