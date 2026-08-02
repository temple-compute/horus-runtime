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
Tests for the resource-scope declaration: where a task's work actually runs.

The property that matters here is that the *default* is right, so an executor
nobody has written yet is still findable by an observer nobody has written yet.
"""

import dataclasses
import os
from typing import ClassVar

import pytest

from horus_builtin.executor.python_exec import PythonExecExecutor
from horus_builtin.executor.python_fn import PythonFunctionExecutor
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.resources import (
    InProcessScope,
    ProcessTreeScope,
    ResourceScope,
)
from horus_runtime.core.target.base import orchestrator_location_id
from horus_runtime.core.target.channel import JobHandle, PollingChannelProcess


def _task(tmp_path: object) -> HorusTask:
    return HorusTask(
        id="t",
        name="t",
        executor=ShellExecutor(),
        runtime=CommandRuntime(command="true"),
        target=LocalTarget(working_directory=str(tmp_path)),
    )


class _SchedulerTarget(LocalTarget):
    """A target that owns the work itself, the way a scheduler does."""

    add_to_registry: ClassVar[bool] = False

    async def resource_scope(
        self, task: object, process: object = None
    ) -> ResourceScope | None:
        """Report the queued job, not whatever the orchestrator spawned."""
        del task, process
        return ResourceScope(kind="batch_job", detail={"job": "42"})


def _scheduler_task(tmp_path: object) -> HorusTask:
    return HorusTask(
        id="t",
        name="t",
        executor=ShellExecutor(),
        runtime=CommandRuntime(command="true"),
        target=_SchedulerTarget(working_directory=str(tmp_path)),
    )


class TestDefaultScope:
    """Executors that spawn a command are covered without writing any code."""

    @pytest.mark.asyncio
    async def test_shell_executor_reports_a_process_tree(
        self, tmp_path: object
    ) -> None:
        """
        ShellExecutor never mentions resources, yet answers correctly.
        """
        scope = await ShellExecutor().resource_scope(_task(tmp_path))
        assert isinstance(scope, ProcessTreeScope)
        assert scope.kind == "process_tree"

    @pytest.mark.asyncio
    async def test_unknown_executor_inherits_the_default(
        self, tmp_path: object
    ) -> None:
        """
        An executor written after the observer is still measurable.

        This is the whole design: no registry entry, no type table, no edit
        anywhere else — it inherits a correct answer.
        """

        class FutureExecutor(BaseExecutor):
            kind: str = "invented_tomorrow"

            async def _execute(self, task: object) -> None:
                """Never called."""

        scope = await FutureExecutor().resource_scope(_task(tmp_path))
        assert isinstance(scope, ProcessTreeScope)

    @pytest.mark.asyncio
    async def test_pid_is_taken_from_the_process_handle(
        self, tmp_path: object
    ) -> None:
        """
        The spawned process's pid is what roots the tree.
        """
        process = PollingChannelProcess(
            _target=LocalTarget(working_directory=str(tmp_path)),
            _handle=JobHandle(pid=4321, job_dir="/tmp/job"),
        )
        scope = await ShellExecutor().resource_scope(_task(tmp_path), process)
        assert isinstance(scope, ProcessTreeScope)
        assert scope.pid == 4321

    @pytest.mark.asyncio
    async def test_absent_process_yields_no_pid_rather_than_raising(
        self, tmp_path: object
    ) -> None:
        """
        Asking before the process exists is legitimate, not an error.
        """
        scope = await ShellExecutor().resource_scope(_task(tmp_path), None)
        assert isinstance(scope, ProcessTreeScope)
        assert scope.pid is None


class TestTargetDeclaredScope:
    """
    A target can reshape the execution, and gets to say so.

    A scheduler target turns a command into a queued job it owns, which the
    executor above it cannot know. Without this, such a target inherits
    ``ProcessTreeScope`` and an observer walks a process that is not the work.
    """

    @pytest.mark.asyncio
    async def test_target_scope_overrides_the_default(
        self, tmp_path: object
    ) -> None:
        """The target's answer replaces the executor's default."""
        task = _scheduler_task(tmp_path)
        scope = await ShellExecutor().resource_scope(task)
        assert scope.kind == "batch_job"

    @pytest.mark.asyncio
    async def test_executor_override_beats_the_target(
        self, tmp_path: object
    ) -> None:
        """
        An executor that reparents its work knows more than the target under
        it, so its override is never second-guessed.
        """
        task = _scheduler_task(tmp_path)
        scope = await PythonExecExecutor().resource_scope(task)
        assert isinstance(scope, InProcessScope)

    @pytest.mark.asyncio
    async def test_plain_target_defers(self, tmp_path: object) -> None:
        """The default target answer changes nothing."""
        target = LocalTarget(working_directory=str(tmp_path))
        assert await target.resource_scope(_task(tmp_path)) is None


class TestInProcessScope:
    """The two in-process executors declare that they cannot be attributed."""

    @pytest.mark.parametrize(
        "executor", [PythonFunctionExecutor(), PythonExecExecutor()]
    )
    @pytest.mark.asyncio
    async def test_reports_in_process(
        self, executor: BaseExecutor, tmp_path: object
    ) -> None:
        """
        They point at the orchestrator's own pid, and say which it is.
        """
        scope = await executor.resource_scope(_task(tmp_path))
        assert isinstance(scope, InProcessScope)
        assert scope.kind == "in_process"
        assert scope.pid == os.getpid()


class TestScopeVocabulary:
    """Scopes name a kind of place, and stay comparable as values."""

    def test_kinds_are_distinct(self) -> None:
        """An observer dispatches on `kind`, so they must not collide."""
        kinds = {
            ProcessTreeScope().kind,
            InProcessScope().kind,
        }
        assert len(kinds) == 2

    def test_scopes_are_frozen_values(self) -> None:
        """
        A scope is a fact about a task, not mutable state to be patched later.
        """
        scope = ProcessTreeScope(pid=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            scope.pid = 2  # type: ignore[misc]


class TestColocation:
    """Locality is derived from location_id, never from a target's type."""

    def test_local_target_is_orchestrator_local(
        self, tmp_path: object
    ) -> None:
        """
        LocalTarget knows nothing about the helper, yet compares equal.
        """
        assert LocalTarget(
            working_directory=str(tmp_path)
        ).is_orchestrator_local

    def test_two_local_targets_are_colocated(self, tmp_path: object) -> None:
        """
        Different working directories on one host are still one place.
        """
        a = LocalTarget(working_directory=str(tmp_path))
        b = LocalTarget(working_directory="/somewhere/else")
        assert a.is_colocated_with(b)

    def test_a_remote_location_is_not_orchestrator_local(
        self, tmp_path: object
    ) -> None:
        """
        A target reporting another location is correctly seen as elsewhere.
        """

        class Remoteish(LocalTarget):
            kind: str = "remoteish_test"

            @property
            def location_id(self) -> str:
                return "ssh://user@elsewhere:22"

        target = Remoteish(working_directory=str(tmp_path))
        assert not target.is_orchestrator_local

    def test_orchestrator_location_matches_local_target_shape(
        self, tmp_path: object
    ) -> None:
        """
        The two are compared as strings, so their shapes must agree.
        """
        target = LocalTarget(working_directory=str(tmp_path))
        assert orchestrator_location_id() == target.location_id
