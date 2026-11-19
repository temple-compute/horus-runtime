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
Unit tests for ``BaseWorkflow.check_output_paths_disjoint``: two tasks may
not write the same output path, or one inside the other's output folder.
"""

from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.core.artifact.base import BaseArtifact


def _task(
    task_id: str, outputs: list[str], inputs: list[str] | None = None
) -> HorusTask:
    """A task writing folders at the given relative *outputs* paths."""

    def folders(prefix: str, paths: list[str]) -> list[BaseArtifact]:
        return cast(
            list[BaseArtifact],
            [
                FolderArtifact(id=f"{prefix}{n}", path=Path(p))
                for n, p in enumerate(paths)
            ],
        )

    return HorusTask(
        id=task_id,
        name=task_id,
        inputs=folders("in", inputs or []),
        outputs=folders("out", outputs),
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=LocalTarget(),
    )


def _workflow(*tasks: HorusTask) -> HorusWorkflow:
    """A workflow holding *tasks* and no edges."""
    return HorusWorkflow(name="wf", tasks=list(tasks))


@pytest.mark.unit
class TestOutputPathsDisjoint:
    """Output paths of different tasks never overlap."""

    def test_nested_output_rejected(self) -> None:
        """A task writing inside another task's output folder is rejected."""
        with pytest.raises(
            ValidationError,
            match="'outer' writes 'results' and task 'inner' writes "
            "'results/x'",
        ):
            _workflow(
                _task("outer", ["results"]), _task("inner", ["results/x"])
            )

    def test_nested_output_rejected_in_any_order(self) -> None:
        """Nesting is caught whichever task is declared first."""
        with pytest.raises(ValidationError, match="cannot be the same"):
            _workflow(_task("inner", ["a/b/c"]), _task("outer", ["a"]))

    def test_equal_output_rejected(self) -> None:
        """Two tasks writing the same path are rejected."""
        with pytest.raises(ValidationError, match="cannot be the same"):
            _workflow(_task("a", ["out"]), _task("b", ["out"]))

    def test_sibling_and_prefix_named_paths_allowed(self) -> None:
        """Paths sharing only a name prefix do not overlap."""
        _workflow(
            _task("a", ["results"]),
            _task("b", ["results2"]),
            _task("c", ["other/results"]),
        )

    def test_in_place_output_allowed(self) -> None:
        """A task writing back to its own input path (an in-place chain)."""
        _workflow(
            _task("make", ["work"]),
            _task("edit1", ["work"], inputs=["work"]),
            _task("edit2", ["work"], inputs=["work"]),
        )

    def test_in_place_does_not_allow_nesting(self) -> None:
        """Reading the outer folder does not allow writing inside it."""
        with pytest.raises(ValidationError, match="cannot be the same"):
            _workflow(
                _task("make", ["results"]),
                _task("fetch", ["results/x.pdb"], inputs=["results"]),
            )

    def test_one_task_may_nest_its_own_outputs(self) -> None:
        """One task's own outputs may nest."""
        _workflow(_task("a", ["out", "out/log"]))

    @pytest.mark.parametrize("child", ["m[0]", "m#1", "m~1", "m/inner"])
    def test_expanded_task_inside_owner_allowed(self, child: str) -> None:
        """Snapshots holding map slots, loop iterations or subworkflow
        children keep loading.
        """
        _workflow(_task("m", ["m_out"]), _task(child, ["m_out/0"]))

    def test_task_with_owner_like_id_is_not_exempt(self) -> None:
        """Only expander separators exempt a task, not a shared id prefix."""
        with pytest.raises(ValidationError, match="cannot be the same"):
            _workflow(_task("m", ["m_out"]), _task("m2", ["m_out/0"]))
