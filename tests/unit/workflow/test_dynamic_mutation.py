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
Unit tests for the runtime DAG-mutation API on BaseWorkflow: add_task,
add_edge, add_artifact, and expand. These let code (typically a running
task's own body, reached via ``BaseTask.workflow``) grow the live DAG mid-run;
the scheduler picks up the mutation automatically because it recomputes
dependencies/scope from ``workflow.tasks``/``workflow.edges`` every loop
iteration.
"""

from pathlib import Path

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.function import FunctionTask
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.dag import CyclicDependencyError
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import (
    ArtifactIdsAreNotUniqueError,
    DuplicateEdgeTargetError,
    TaskIdsAreNotUniqueError,
    UnknownEdgeEndpointError,
)


def _task(
    task_id: str,
    *,
    inputs: list[BaseArtifact] | None = None,
    outputs: list[BaseArtifact] | None = None,
    target: LocalTarget | None = None,
) -> HorusTask:
    """Build a minimal HorusTask for DAG-mutation tests."""
    return HorusTask(
        id=task_id,
        name=task_id,
        inputs=inputs or [],
        outputs=outputs or [],
        runtime=CommandRuntime(command="echo hi"),
        executor=ShellExecutor(),
        target=target or LocalTarget(),
    )


def _edge(
    source: str, source_output: str, target: str, target_input: str
) -> WorkflowEdge:
    """Build a WorkflowEdge from its four endpoint ids."""
    return WorkflowEdge(
        source=source,
        source_output=source_output,
        target=target,
        target_input=target_input,
    )


@pytest.mark.unit
class TestAddArtifact:
    """add_artifact appends a root artifact with incremental id validation."""

    def test_add_artifact_appends_and_bumps_revision(
        self, tmp_path: Path
    ) -> None:
        """A fresh root artifact is appended and the revision advances."""
        wf = HorusWorkflow(name="wf")
        before = wf._revision

        artifact = FileArtifact(id="root", path=tmp_path / "root.txt")
        wf.add_artifact(artifact)

        assert wf.artifacts == [artifact]
        assert wf._revision == before + 1

    def test_add_artifact_duplicate_id_raises_and_does_not_append(
        self, tmp_path: Path
    ) -> None:
        """A colliding root artifact id raises and appends nothing."""
        existing = FileArtifact(id="root", path=tmp_path / "a.txt")
        wf = HorusWorkflow(name="wf", artifacts=[existing])
        before = wf._revision

        with pytest.raises(ArtifactIdsAreNotUniqueError):
            wf.add_artifact(FileArtifact(id="root", path=tmp_path / "b.txt"))

        assert wf.artifacts == [existing]
        assert wf._revision == before


@pytest.mark.unit
class TestAddTask:
    """add_task appends a task with incremental id validation and anchoring."""

    def test_add_task_appends_and_bumps_revision(self) -> None:
        """A task with a fresh id is appended and the revision advances."""
        wf = HorusWorkflow(name="wf", tasks=[_task("t1")])
        before = wf._revision

        t2 = _task("t2")
        wf.add_task(t2)

        assert [t.id for t in wf.tasks] == ["t1", "t2"]
        assert wf._revision == before + 1

    def test_add_task_duplicate_id_raises_and_does_not_append(self) -> None:
        """A colliding task id raises and appends nothing."""
        wf = HorusWorkflow(name="wf", tasks=[_task("t1")])
        before = wf._revision

        with pytest.raises(TaskIdsAreNotUniqueError):
            wf.add_task(_task("t1"))

        assert len(wf.tasks) == 1
        assert wf._revision == before

    def test_add_task_duplicate_own_output_ids_raises(
        self, tmp_path: Path
    ) -> None:
        """A task declaring two outputs with the same id is rejected."""
        wf = HorusWorkflow(name="wf")
        dup_task = _task(
            "t1",
            outputs=[
                FileArtifact(id="out", path=tmp_path / "a.txt"),
                FileArtifact(id="out", path=tmp_path / "b.txt"),
            ],
        )

        with pytest.raises(ArtifactIdsAreNotUniqueError):
            wf.add_task(dup_task)

        assert wf.tasks == []

    def test_add_task_anchors_paths_and_inherits_working_directory(
        self, tmp_path: Path
    ) -> None:
        """
        A task added mid-run is anchored exactly like a construction-time
        task: declared artifact paths become absolute under the run root,
        and its co-located target inherits the orchestrator's working
        directory (mirrors TestResolveRunPaths / TestOrchestratorWorking-
        DirectoryPropagation in test_run_layout.py / test_builtin_workflow.py).
        """
        wf = HorusWorkflow(
            name="wf",
            orchestrator_target=LocalTarget(
                working_directory="workflow_results"
            ),
        )
        wf._base_directory = tmp_path
        # Simulate a run already under way: paths anchored once already.
        wf._resolve_run_paths()

        produced = FileArtifact(id="out", path=Path("results/out.txt"))
        new_task = _task("added", outputs=[produced], target=LocalTarget())

        wf.add_task(new_task)

        run_root = (tmp_path / "workflow_results").resolve()
        assert produced.path == (run_root / "results/out.txt").resolve()
        assert new_task.target.working_directory == run_root.as_posix()
        assert new_task.working_dir == (run_root / "added").as_posix()


@pytest.mark.unit
class TestAddEdge:
    """add_edge appends an edge with incremental endpoint/cycle validation."""

    def test_add_edge_appends_and_bumps_revision(self, tmp_path: Path) -> None:
        """A well-formed edge is appended and the revision advances."""
        producer = _task(
            "producer",
            outputs=[FileArtifact(id="out", path=tmp_path / "out.txt")],
        )
        consumer = _task(
            "consumer",
            inputs=[FileArtifact(id="in", path=tmp_path / "in.txt")],
        )
        wf = HorusWorkflow(name="wf", tasks=[producer, consumer])
        before = wf._revision

        edge = _edge("producer", "out", "consumer", "in")
        wf.add_edge(edge)

        assert wf.edges == [edge]
        assert wf._revision == before + 1

    def test_add_edge_unknown_target_raises_and_does_not_append(self) -> None:
        """An edge into a non-existent task raises and appends nothing."""
        wf = HorusWorkflow(name="wf", tasks=[_task("t1")])

        with pytest.raises(UnknownEdgeEndpointError):
            wf.add_edge(_edge("t1", "out", "missing", "in"))

        assert wf.edges == []

    def test_add_edge_duplicate_target_raises_and_does_not_append(
        self, tmp_path: Path
    ) -> None:
        """A second edge into an already-fed input raises and appends none."""
        a = _task(
            "a", outputs=[FileArtifact(id="a_out", path=tmp_path / "a.txt")]
        )
        c = _task(
            "c", outputs=[FileArtifact(id="c_out", path=tmp_path / "c.txt")]
        )
        b = _task(
            "b", inputs=[FileArtifact(id="b_in", path=tmp_path / "b.txt")]
        )
        first = _edge("a", "a_out", "b", "b_in")
        wf = HorusWorkflow(name="wf", tasks=[a, b, c], edges=[first])

        with pytest.raises(DuplicateEdgeTargetError):
            wf.add_edge(_edge("c", "c_out", "b", "b_in"))

        assert wf.edges == [first]

    def test_add_edge_cycle_raises_and_does_not_append(
        self, tmp_path: Path
    ) -> None:
        """A -> B already exists; adding B -> A would close a cycle."""
        a = _task(
            "a",
            inputs=[FileArtifact(id="a_in", path=tmp_path / "a_in.txt")],
            outputs=[FileArtifact(id="a_out", path=tmp_path / "a_out.txt")],
        )
        b = _task(
            "b",
            inputs=[FileArtifact(id="b_in", path=tmp_path / "b_in.txt")],
            outputs=[FileArtifact(id="b_out", path=tmp_path / "b_out.txt")],
        )
        forward = _edge("a", "a_out", "b", "b_in")
        wf = HorusWorkflow(name="wf", tasks=[a, b], edges=[forward])

        backward = _edge("b", "b_out", "a", "a_in")
        with pytest.raises(CyclicDependencyError):
            wf.add_edge(backward)

        assert wf.edges == [forward]


@pytest.mark.unit
class TestExpand:
    """expand() commits a batch of tasks/edges/artifacts atomically."""

    def test_expand_commits_whole_batch_atomically(
        self, tmp_path: Path
    ) -> None:
        """A valid batch of task + edge + root artifact commits at once."""
        root = _task(
            "root",
            outputs=[FileArtifact(id="root_out", path=tmp_path / "r.txt")],
        )
        wf = HorusWorkflow(name="wf", tasks=[root])
        before = wf._revision

        mapped = _task(
            "map1", inputs=[FileArtifact(id="in", path=tmp_path / "m.txt")]
        )
        edge = _edge("root", "root_out", "map1", "in")
        root_artifact = FileArtifact(id="side", path=tmp_path / "side.txt")

        wf.expand(tasks=[mapped], edges=[edge], artifacts=[root_artifact])

        assert [t.id for t in wf.tasks] == ["root", "map1"]
        assert wf.edges == [edge]
        assert wf.artifacts == [root_artifact]
        # Bumped once for the whole batch, not once per item.
        assert wf._revision == before + 1

    def test_expand_invalid_batch_commits_nothing(self) -> None:
        """
        One bad edge in the batch (references a task id that isn't part of
        this expand call) must roll back the whole batch: neither the new
        task nor the edge is appended, and the graph is left unchanged.
        """
        root = _task("root")
        wf = HorusWorkflow(name="wf", tasks=[root])
        tasks_before = list(wf.tasks)
        edges_before = list(wf.edges)
        artifacts_before = list(wf.artifacts)
        revision_before = wf._revision

        mapped = _task("map1")
        bad_edge = _edge("root", "does-not-exist", "map1", "does-not-exist")

        with pytest.raises(UnknownEdgeEndpointError):
            wf.expand(tasks=[mapped], edges=[bad_edge])

        assert wf.tasks == tasks_before
        assert wf.edges == edges_before
        assert wf.artifacts == artifacts_before
        assert wf._revision == revision_before

    def test_expand_edges_may_reference_tasks_added_in_the_same_batch(
        self, tmp_path: Path
    ) -> None:
        """
        The whole batch validates against the *combined* graph, so an edge
        may wire two tasks that are both being added in this same call.
        """
        wf = HorusWorkflow(name="wf")

        producer = _task(
            "producer",
            outputs=[FileArtifact(id="out", path=tmp_path / "out.txt")],
        )
        consumer = _task(
            "consumer",
            inputs=[FileArtifact(id="in", path=tmp_path / "in.txt")],
        )
        edge = _edge("producer", "out", "consumer", "in")

        wf.expand(tasks=[producer, consumer], edges=[edge])

        assert {t.id for t in wf.tasks} == {"producer", "consumer"}
        assert wf.edges == [edge]


@pytest.mark.unit
class TestTaskWorkflowProperty:
    """BaseTask.workflow reaches the live workflow via HorusContext."""

    def test_workflow_property_is_none_outside_a_run(
        self, horus_context: HorusContext
    ) -> None:
        """Outside a workflow run the context workflow is None."""
        del horus_context
        task = FunctionTask(
            id="t", name="t", runtime=PythonFunctionRuntime(func=lambda: None)
        )
        assert task.workflow is None


@pytest.mark.unit
class TestDynamicMutationEndToEnd:
    """
    A running task can grow the DAG it belongs to, and the scheduler picks
    the addition up automatically.
    """

    async def test_task_injected_at_runtime_actually_runs(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A trigger task injects a downstream task + edge from its own body;
        the scheduler dispatches the injected task and it reaches COMPLETED,
        materializing its output.
        """
        del horus_context

        def downstream_fn(in_file: FileArtifact, marker: FileArtifact) -> None:
            marker.path.write_text(in_file.path.read_text().upper())

        def trigger_fn(task: BaseTask, out: FileArtifact) -> None:
            out.path.write_text("hello")

            wf = task.workflow
            assert wf is not None

            downstream = FunctionTask(
                id="downstream",
                name="downstream",
                runtime=PythonFunctionRuntime(func=downstream_fn),
                inputs=[
                    FileArtifact(id="in_file", path=Path("trigger_out.txt"))
                ],
                outputs=[FileArtifact(id="marker", path=Path("marker.txt"))],
            )
            wf.add_task(downstream)
            wf.add_edge(_edge(task.id, "out", "downstream", "in_file"))

        trigger = FunctionTask(
            id="trigger",
            name="trigger",
            runtime=PythonFunctionRuntime(func=trigger_fn),
            outputs=[FileArtifact(id="out", path=Path("trigger_out.txt"))],
        )

        wf = HorusWorkflow(
            name="dynamic",
            tasks=[trigger],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )

        await wf.run(trigger_id="trigger")

        assert wf.status.value == "completed"
        assert [t.id for t in wf.tasks] == ["trigger", "downstream"]

        downstream_task = next(t for t in wf.tasks if t.id == "downstream")
        assert downstream_task.status == TaskStatus.COMPLETED

        marker_path = (tmp_path / "marker.txt").resolve()
        assert marker_path.exists()
        assert marker_path.read_text() == "HELLO"
