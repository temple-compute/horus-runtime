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
Tests for ExternalPythonFunctionExecutor.

These run the real thing: a real subprocess, on a real local target, with a
real cloudpickle payload. Mocking the subprocess away would test the mock,
and the whole point of this executor is that the work leaves the process.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.python_fn_external import (
    ExternalPythonFunctionExecutor,
)
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.function import FunctionTask
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.resources import ProcessTreeScope
from horus_runtime.core.task.exceptions import TaskExecutionError


def make_task(
    tmp_path: Path,
    func: Callable[..., Any],
    *,
    inputs: list[BaseArtifact] | None = None,
) -> FunctionTask:
    """Build a FunctionTask wired to the out-of-process executor."""
    return FunctionTask(
        id="external_fn",
        name="external_fn",
        runtime=PythonFunctionRuntime(func=func),
        executor=ExternalPythonFunctionExecutor(),
        target=LocalTarget(working_directory=tmp_path.as_posix()),
        inputs=inputs or [],
    )


@pytest.mark.unit
class TestExternalPythonFunctionExecutor:
    """End-to-end behaviour of the out-of-process function executor."""

    async def test_runs_in_another_process_and_returns_artifacts(
        self, tmp_path: Path
    ) -> None:
        """
        The function really runs elsewhere, sees its inputs, and its returned
        side artifacts come back.
        """
        source = tmp_path / "input.txt"
        source.write_text("payload")
        artifact = FileArtifact(id="data", path=source)

        def work(data: FileArtifact) -> FileArtifact:
            out = Path("out.txt")
            out.write_text(f"{os.getpid()}:{Path(data.path).read_text()}")
            return FileArtifact(id="result", path=out.resolve())

        task = make_task(tmp_path, work, inputs=[artifact])
        await task.executor.execute(task)

        results = [a for a in task.side_artifacts if a.id == "result"]
        assert len(results) == 1
        child_pid, _, content = (
            Path(results[0].path).read_text().partition(":")
        )
        assert content == "payload"
        assert int(child_pid) != os.getpid()

    async def test_closure_over_local_state(self, tmp_path: Path) -> None:
        """
        A closure over local state survives the trip. This is what plain
        pickle cannot do and why cloudpickle is a dependency.
        """
        factor = 21
        target_file = tmp_path / "closure.txt"

        def work() -> None:
            target_file.write_text(str(factor * 2))

        task = make_task(tmp_path, work)
        await task.executor.execute(task)

        assert target_file.read_text() == "42"

    async def test_failure_surfaces_original_exception(
        self, tmp_path: Path
    ) -> None:
        """
        A raising function fails the task with its own exception type and
        message, plus the target-side traceback.
        """

        def work() -> None:
            raise KeyError("missing-thing")

        task = make_task(tmp_path, work)
        with pytest.raises(KeyError) as excinfo:
            await task.executor.execute(task)

        assert "missing-thing" in str(excinfo.value)
        notes = "".join(getattr(excinfo.value, "__notes__", []))
        assert "KeyError: 'missing-thing'" in notes
        assert "in work" in notes

    async def test_rejects_a_function_that_wants_the_live_task(
        self, tmp_path: Path
    ) -> None:
        """
        The live task cannot cross a process boundary, so asking for it is
        refused up front rather than handed a useless copy.
        """

        def work(task: object) -> None:
            del task

        task = make_task(tmp_path, work)
        with pytest.raises(TaskExecutionError, match="'task' argument"):
            await task.executor.execute(task)

    async def test_missing_interpreter_reports_the_task(
        self, tmp_path: Path
    ) -> None:
        """
        A target with no usable interpreter fails as a task error naming the
        task, not as an unpickling crash in the orchestrator.
        """

        def work() -> None:
            return None

        task = make_task(tmp_path, work)
        assert isinstance(task.executor, ExternalPythonFunctionExecutor)
        task.executor.python = "definitely-not-a-python-interpreter"
        with pytest.raises(TaskExecutionError, match="produced no result"):
            await task.executor.execute(task)

    async def test_resource_scope_is_the_inherited_process_tree(
        self, tmp_path: Path
    ) -> None:
        """
        The executor overrides nothing: it gets a real, measurable process
        tree from BaseExecutor purely by running its work as a command.
        """
        assert "resource_scope" not in vars(ExternalPythonFunctionExecutor)

        target = LocalTarget(working_directory=tmp_path.as_posix())
        proc = await target.run_command("sleep 0.1", cwd=tmp_path.as_posix())
        try:
            task = make_task(tmp_path, lambda: None)
            scope = await task.executor.resource_scope(task, proc)
        finally:
            await proc.wait()

        assert isinstance(scope, ProcessTreeScope)
        assert scope.pid is not None
        assert scope.pid > 0

    def test_registered_under_its_kind(self) -> None:
        """The executor is discoverable through the registry."""
        assert (
            BaseExecutor.registry["python_function_external"]
            is ExternalPythonFunctionExecutor
        )
