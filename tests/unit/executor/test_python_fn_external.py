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

import json
import os
import pickle
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor.python_fn_external import (
    ExternalPythonFunctionExecutor,
)
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.function import FunctionTask
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.resources import ProcessTreeScope
from horus_runtime.core.task.exceptions import (
    TargetLostError,
    TaskExecutionError,
)


def make_task(
    tmp_path: Path,
    func: Callable[..., Any],
    *,
    inputs: list[BaseArtifact] | None = None,
    outputs: list[BaseArtifact] | None = None,
) -> FunctionTask:
    """Build a FunctionTask wired to the out-of-process executor."""
    return FunctionTask(
        id="external_fn",
        name="external_fn",
        runtime=PythonFunctionRuntime(func=func),
        executor=ExternalPythonFunctionExecutor(),
        target=LocalTarget(working_directory=tmp_path.as_posix()),
        inputs=inputs or [],
        outputs=outputs or [],
    )


def file_output(tmp_path: Path) -> list[BaseArtifact]:
    """A declared file output, which is what allows returning files."""
    return [FileArtifact(id="declared", path=tmp_path / "declared.txt")]


def forged(descriptor: object) -> AsyncMock:
    """A ``get_file`` that hands back an outcome naming *descriptor*."""
    outcome = {"ok": True, "value": {"__horus_artifact__": descriptor}}
    return AsyncMock(return_value=json.dumps(outcome).encode())


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

        task = make_task(
            tmp_path, work, inputs=[artifact], outputs=file_output(tmp_path)
        )
        await task.executor.execute(task)

        results = [a for a in task.side_artifacts if a.id == "result"]
        assert len(results) == 1
        # A local copy, not the path the target named.
        assert Path(results[0].path) != Path(task.working_dir) / "out.txt"
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

    async def test_failure_surfaces_remote_traceback(
        self, tmp_path: Path
    ) -> None:
        """
        A raising function fails the task with the exception type, message
        and target-side traceback as text.
        """

        def work() -> None:
            raise KeyError("missing-thing")

        task = make_task(tmp_path, work)
        with pytest.raises(TaskExecutionError) as excinfo:
            await task.executor.execute(task)

        assert "KeyError: 'missing-thing'" in str(excinfo.value)
        assert "in work" in str(excinfo.value)

    async def test_non_json_return_fails_clearly(self, tmp_path: Path) -> None:
        """A return value that cannot travel as JSON fails the task."""

        def work() -> object:
            return object()

        task = make_task(tmp_path, work)
        with pytest.raises(TaskExecutionError, match="not JSON-serialisable"):
            await task.executor.execute(task)

    async def test_returned_folder_comes_back(self, tmp_path: Path) -> None:
        """A declared folder kind can be returned and is pulled back."""

        def work() -> FolderArtifact:
            Path("plots").mkdir()
            Path("plots/a.txt").write_text("a")
            return FolderArtifact(id="plots", path=Path("plots").resolve())

        task = make_task(
            tmp_path,
            work,
            outputs=[FolderArtifact(id="out", path=tmp_path / "out")],
        )
        await task.executor.execute(task)

        (plots,) = [a for a in task.side_artifacts if a.id == "plots"]
        assert (Path(plots.path) / "a.txt").read_text() == "a"

    async def test_returned_file_outside_working_dir_is_refused(
        self, tmp_path: Path
    ) -> None:
        """A function cannot name an arbitrary file as its result."""

        def work() -> FileArtifact:
            return FileArtifact(id="x", path=Path("/etc/passwd"))

        task = make_task(tmp_path, work, outputs=file_output(tmp_path))
        with pytest.raises(TaskExecutionError, match="not accepted"):
            await task.executor.execute(task)
        assert task.side_artifacts == []

    async def test_returned_kind_must_be_declared(
        self, tmp_path: Path
    ) -> None:
        """Without a declared file output, returning a file is refused."""

        def work() -> FileArtifact:
            Path("out.txt").write_text("x")
            return FileArtifact(id="x", path=Path("out.txt").resolve())

        task = make_task(tmp_path, work)
        with pytest.raises(TaskExecutionError, match="not accepted"):
            await task.executor.execute(task)

    @pytest.mark.parametrize(
        "descriptor",
        [
            {"kind": "file", "id": "x", "path": "/proc/self/environ"},
            {"kind": "file", "id": "x", "path": "../../etc/passwd"},
            {"kind": "file", "id": "x", "path": "a/../../x"},
            {"kind": "pickle", "id": "x", "path": "out.pkl"},
            {"kind": "file", "id": 3, "path": "out.txt"},
            {"kind": "file", "id": "x"},
            {"kind": ["file"], "id": "x", "path": "out.txt"},
            "not-a-dict",
        ],
    )
    async def test_forged_descriptors_are_refused(
        self, tmp_path: Path, descriptor: object
    ) -> None:
        """An outcome written on the target cannot name orchestrator files."""
        task = make_task(tmp_path, lambda: None, outputs=file_output(tmp_path))
        with (
            patch.object(LocalTarget, "get_file", new=forged(descriptor)),
            pytest.raises(TaskExecutionError, match="not accepted"),
        ):
            await task.executor.execute(task)
        assert task.side_artifacts == []

    async def test_symlinked_result_is_refused(self, tmp_path: Path) -> None:
        """A relative path that is a symlink out of the workdir is refused."""
        task = make_task(tmp_path, lambda: None, outputs=file_output(tmp_path))
        workdir = Path(task.working_dir)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "link.txt").symlink_to("/etc/passwd")
        (workdir / "sub").symlink_to("/etc")
        for path in ("link.txt", "sub/passwd"):
            descriptor = {"kind": "file", "id": "x", "path": path}
            with (
                patch.object(LocalTarget, "get_file", new=forged(descriptor)),
                pytest.raises(TaskExecutionError, match="does not exist"),
            ):
                await task.executor.execute(task)
        assert task.side_artifacts == []

    async def test_result_file_is_never_unpickled(
        self, tmp_path: Path
    ) -> None:
        """
        Bytes read back from the target are decoded as data only, so a pickle
        written there cannot run code in the orchestrator.
        """
        marker = tmp_path / "unpickled"

        class Evil:
            def __reduce__(self) -> tuple[Any, ...]:
                return (marker.write_text, ("x",))

        task = make_task(tmp_path, lambda: None)
        with (
            patch.object(
                LocalTarget,
                "get_file",
                new=AsyncMock(return_value=pickle.dumps(Evil())),
            ),
            pytest.raises(TaskExecutionError, match="produced no result"),
        ):
            await task.executor.execute(task)

        assert not marker.exists()

    async def test_interpreter_is_not_a_shell_fragment(
        self, tmp_path: Path
    ) -> None:
        """The ``python`` field is one quoted word, never extra shell."""
        marker = tmp_path / "injected"
        task = make_task(tmp_path, lambda: None)
        assert isinstance(task.executor, ExternalPythonFunctionExecutor)
        task.executor.python = f"touch {marker} #"
        with pytest.raises(TaskExecutionError, match="produced no result"):
            await task.executor.execute(task)

        assert not marker.exists()

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

    async def test_lost_target_keeps_its_type(self, tmp_path: Path) -> None:
        """
        A lost machine while reading the outcome stays a ``TargetLostError``,
        so the scheduler can retry the task.
        """
        task = make_task(tmp_path, lambda: None)
        with (
            patch.object(
                LocalTarget,
                "get_file",
                new=AsyncMock(side_effect=TargetLostError("node gone")),
            ),
            pytest.raises(TargetLostError, match="node gone"),
        ):
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
