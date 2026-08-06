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
End-to-end tests for workflow-owned value artifacts.

A root artifact that carries an authored ``value`` (NumberArtifact,
BooleanArtifact, StringArtifact) is indistinguishable at runtime from a file
artifact: it is materialized the first time a task consumes it, transferred
through the ordinary path, and substituted into task commands like any other
input. These tests pin that behaviour down.
"""

from pathlib import Path

import pytest

from horus_builtin.artifact.boolean import BooleanArtifact
from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.number import NumberArtifact
from horus_builtin.artifact.string import StringArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.workflow.edge import WorkflowEdge


def _value_root(
    artifact_id: str,
    *,
    kind: str,
    value: object,
) -> object:
    """One value artifact usable both as a root and as a task input."""
    return {
        "number": NumberArtifact,
        "boolean": BooleanArtifact,
        "string": StringArtifact,
    }[kind](id=artifact_id, path=Path(f"roots/{artifact_id}"), value=value)


def _consume_workflow(tmp_path: Path) -> HorusWorkflow:
    """
    A single task that cats every value root into one report, so a run
    proves each value was materialized and substituted.
    """
    roots = [
        _value_root("n", kind="number", value=42),
        _value_root("b", kind="boolean", value=True),
        _value_root("s", kind="string", value="hello"),
    ]
    task = HorusTask(
        id="consume",
        name="consume",
        runtime=CommandRuntime(command="printf '%s|%s|%s' $n $b $s > $report"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[r.model_copy(deep=True) for r in roots],  # type: ignore[attr-defined]
        outputs=[FileArtifact(id="report", path=Path("report.txt"))],
    )
    wf = HorusWorkflow(
        name="value-roots",
        tasks=[task],
        artifacts=roots,  # type: ignore[arg-type]
        edges=[
            WorkflowEdge(
                source="artifact-n",
                source_output="n",
                target="consume",
                target_input="n",
            ),
            WorkflowEdge(
                source="artifact-b",
                source_output="b",
                target="consume",
                target_input="b",
            ),
            WorkflowEdge(
                source="artifact-s",
                source_output="s",
                target="consume",
                target_input="s",
            ),
        ],
    )
    wf._base_directory = tmp_path
    return wf


@pytest.mark.unit
class TestValueRootsMaterializeAndTransfer:
    """Value roots are materialized, substituted, and survive reloads."""

    async def test_values_reach_the_task_command(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Each value root is materialized and substituted into the shell."""
        del horus_context
        wf = _consume_workflow(tmp_path)

        await wf.run(trigger_id="consume")

        assert wf.status.value == "completed"
        report = next(t for t in wf.tasks if t.id == "consume").outputs[0]
        assert report.path.read_text().strip() == "42|true|hello"

    async def test_root_files_exist_after_run(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The materialized bytes land on disk under the run's base dir."""
        del horus_context
        wf = _consume_workflow(tmp_path)

        await wf.run(trigger_id="consume")

        assert (tmp_path / "roots/n").read_text() == "42"
        assert (tmp_path / "roots/b").read_text() == "true"
        assert (tmp_path / "roots/s").read_text() == "hello"

    async def test_values_survive_dump_reload(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A workflow document that carries authored values round-trips through
        model_validate without losing them, so the orchestrator sees the same
        values the editor saved.
        """
        del horus_context
        wf = _consume_workflow(tmp_path)

        reloaded = HorusWorkflow.model_validate(wf.model_dump(mode="json"))
        root_ids = {a.id for a in reloaded.artifacts}
        assert root_ids == {"n", "b", "s"}
        values = {a.id: getattr(a, "value", None) for a in reloaded.artifacts}
        assert values == {"n": 42, "b": True, "s": "hello"}
