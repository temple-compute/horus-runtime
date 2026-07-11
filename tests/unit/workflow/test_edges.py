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
Unit tests for edge-native DAG resolution and transfer source resolution.

Edges are the sole source of truth for the DAG: a consumer's input keeps its
own id/name while depending on a producer's differently-named output. A
workflow with no edges has independent tasks (no dependencies).
"""

from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.dag import (
    CyclicDependencyError,
    build_dependencies,
    execution_plan,
)
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.transfer.strategy import BaseTransferStrategy
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import (
    ArtifactIdsAreNotUniqueError,
    DuplicateEdgeTargetError,
    UnknownEdgeEndpointError,
)


def _task(
    *,
    task_id: str,
    inputs: list[FileArtifact],
    outputs: list[FileArtifact],
    target: LocalTarget | None = None,
    definition_id: str | None = None,
) -> HorusTask:
    """Build a minimal HorusTask for DAG/transfer tests."""
    return HorusTask(
        id=task_id,
        definition_id=definition_id,
        name=task_id,
        inputs=cast(list[BaseArtifact], inputs),
        outputs=cast(list[BaseArtifact], outputs),
        runtime=CommandRuntime(command="echo hi"),
        executor=ShellExecutor(),
        target=target or LocalTarget(),
    )


def _edge(
    source: str,
    source_output: str,
    target: str,
    target_input: str,
    *,
    transfer: bool = True,
) -> WorkflowEdge:
    """Build a WorkflowEdge from the four endpoint ids."""
    return WorkflowEdge(
        source=source,
        source_output=source_output,
        target=target,
        target_input=target_input,
        transfer=transfer,
    )


class _Captured:
    """Records what ``transfer_artifacts`` hands to the transfer strategy."""

    def __init__(self) -> None:
        self.source: object = None
        self.dest: object = None
        self.artifact_id: str | None = None
        captured = self

        class _Strategy:
            async def transfer(
                self, artifact: object, source: object, dest: object
            ) -> None:
                captured.source = source
                captured.dest = dest
                captured.artifact_id = getattr(artifact, "id", None)

        self.strategy = _Strategy


def _capture_transfer() -> _Captured:
    """Return a recorder whose ``.strategy`` can patch get_from_registry."""
    return _Captured()


@pytest.mark.unit
class TestArtifactName:
    """The display ``name`` defaults to ``id`` but is otherwise preserved."""

    def test_name_defaults_to_id(self, tmp_path: Path) -> None:
        """A name-less artifact reports its id as the display name."""
        art = FileArtifact(id="pdb_in", path=tmp_path / "x.txt")
        assert art.name == "pdb_in"

    def test_name_is_kept_when_given(self, tmp_path: Path) -> None:
        """An explicit name survives construction."""
        art = FileArtifact(
            id="in_1", name="Parsed Chains JSON", path=tmp_path / "x.txt"
        )
        assert art.name == "Parsed Chains JSON"


@pytest.mark.unit
class TestEdgeExecutionPlan:
    """``execution_plan`` ordering driven entirely by edges."""

    def test_edge_orders_producer_before_consumer_distinct_ids(
        self, tmp_path: Path
    ) -> None:
        """Input and output ids differ; the edge still creates the dep."""
        producer = _task(
            task_id="producer",
            inputs=[],
            outputs=[FileArtifact(id="out_parsed", path=tmp_path / "p.txt")],
        )
        consumer = _task(
            task_id="consumer",
            inputs=[FileArtifact(id="in_pdb", path=tmp_path / "c.txt")],
            outputs=[],
        )
        edges = [_edge("producer", "out_parsed", "consumer", "in_pdb")]
        # Listed consumer-first to prove ordering follows edges.
        plan = execution_plan(
            [consumer, producer], trigger_id="producer", edges=edges
        )
        assert plan == ["producer", "consumer"]

    def test_cycle_via_edges_raises(self, tmp_path: Path) -> None:
        """A cycle expressed purely through edges is detected."""
        a = _task(
            task_id="a",
            inputs=[FileArtifact(id="a_in", path=tmp_path / "a.txt")],
            outputs=[FileArtifact(id="a_out", path=tmp_path / "ao.txt")],
        )
        b = _task(
            task_id="b",
            inputs=[FileArtifact(id="b_in", path=tmp_path / "b.txt")],
            outputs=[FileArtifact(id="b_out", path=tmp_path / "bo.txt")],
        )
        edges = [
            _edge("a", "a_out", "b", "b_in"),
            _edge("b", "b_out", "a", "a_in"),
        ]
        with pytest.raises(CyclicDependencyError):
            execution_plan([a, b], trigger_id="a", edges=edges)

    def test_root_sourced_edge_is_not_a_dependency(
        self, tmp_path: Path
    ) -> None:
        """An edge whose source is a root artifact adds no task dependency."""
        consumer = _task(
            task_id="consumer",
            inputs=[FileArtifact(id="in_pdb", path=tmp_path / "c.txt")],
            outputs=[],
        )
        edges = [_edge("artifact-root_pdb", "root_pdb", "consumer", "in_pdb")]
        plan = execution_plan([consumer], trigger_id="consumer", edges=edges)
        assert plan == ["consumer"]

    def test_no_edges_means_independent_tasks(self, tmp_path: Path) -> None:
        """
        With no edges there is no dependency inference: matching input/output
        ids no longer creates an ordering, so only the trigger is in scope.
        """
        shared = tmp_path / "shared.txt"
        producer = _task(
            task_id="producer",
            inputs=[],
            outputs=[FileArtifact(id="shared", path=shared)],
        )
        consumer = _task(
            task_id="consumer",
            inputs=[FileArtifact(id="shared", path=shared)],
            outputs=[],
        )
        plan = execution_plan([consumer, producer], trigger_id="producer")
        assert plan == ["producer"]


@pytest.mark.unit
class TestReusableTasks:
    """The same reusable task may be placed more than once in a workflow."""

    def test_same_definition_placed_twice_builds_and_orders(
        self, tmp_path: Path
    ) -> None:
        """
        Two placements share ``definition_id`` and identical output ids but get
        distinct ``id``s. The workflow validates, the DAG orders both after the
        shared producer, and their working dirs differ (no on-disk collision).
        """
        src = _task(
            task_id="src",
            inputs=[],
            outputs=[FileArtifact(id="data", path=tmp_path / "data.txt")],
        )
        dup1 = _task(
            task_id="dup-1",
            definition_id="reusable",
            inputs=[FileArtifact(id="in", path=tmp_path / "i1.txt")],
            outputs=[FileArtifact(id="result", path=tmp_path / "r1.txt")],
        )
        dup2 = _task(
            task_id="dup-2",
            definition_id="reusable",
            inputs=[FileArtifact(id="in", path=tmp_path / "i2.txt")],
            outputs=[FileArtifact(id="result", path=tmp_path / "r2.txt")],
        )
        edges = [
            _edge("src", "data", "dup-1", "in"),
            _edge("src", "data", "dup-2", "in"),
        ]
        # Construction exercises check_unique_artifact_ids: identical "result"
        # output ids across placements are allowed (unique within each task).
        wf = HorusWorkflow(name="reuse", tasks=[dup1, dup2, src], edges=edges)
        plan = execution_plan(wf.tasks, trigger_id="src", edges=wf.edges)
        assert plan == ["src", "dup-1", "dup-2"]
        assert dup1.definition_id == dup2.definition_id == "reusable"
        assert dup1.working_dir != dup2.working_dir

    def test_duplicate_output_id_within_one_task_raises(
        self, tmp_path: Path
    ) -> None:
        """Output ids must still be unique *within* a single task."""
        bad = _task(
            task_id="bad",
            inputs=[],
            outputs=[
                FileArtifact(id="dup", path=tmp_path / "a.txt"),
                FileArtifact(id="dup", path=tmp_path / "b.txt"),
            ],
        )
        with pytest.raises(ArtifactIdsAreNotUniqueError):
            HorusWorkflow(name="bad", tasks=[bad])


@pytest.mark.unit
class TestEdgeTransferResolution:
    """``transfer_artifacts`` resolves the source target via edges."""

    async def test_input_transfers_with_producer_output_id_and_path(
        self, tmp_path: Path
    ) -> None:
        """
        The artifact handed to the strategy carries the producer OUTPUT id
        (so the blob lookup hits), and the consumer input's path is updated to
        the producer's source path — even though their ids/paths differ.
        """
        producer_target = LocalTarget()
        consumer_target = LocalTarget()
        producer_path = tmp_path / "p.txt"
        producer = _task(
            task_id="producer",
            inputs=[],
            outputs=[FileArtifact(id="out_parsed", path=producer_path)],
            target=producer_target,
        )
        consumer = _task(
            task_id="consumer",
            inputs=[FileArtifact(id="in_pdb", path=tmp_path / "c.txt")],
            outputs=[],
            target=consumer_target,
        )
        wf = HorusWorkflow(
            name="edge_transfer",
            tasks=[consumer, producer],
            edges=[_edge("producer", "out_parsed", "consumer", "in_pdb")],
        )

        captured = _capture_transfer()
        with patch.object(
            BaseTransferStrategy,
            "get_from_registry",
            return_value=captured.strategy,
        ):
            await wf.transfer_artifacts(consumer)

        assert captured.source is producer_target
        assert captured.dest is consumer_target
        # Strategy got the source (output) id, not the consumer input id.
        assert captured.artifact_id == "out_parsed"
        # Consumer input now points at the producer's materialized path.
        assert consumer.inputs[0].path == producer_path.resolve()
        # The consumer input keeps its own id (the template key).
        assert consumer.inputs[0].id == "in_pdb"

    async def test_root_input_transfers_with_root_id_from_orchestrator(
        self, tmp_path: Path
    ) -> None:
        """
        A root-sourced input resolves to the orchestrator and is fetched using
        the ROOT artifact id (where the upload is stored), not the input id.
        """
        root_path = tmp_path / "root.txt"
        consumer = _task(
            task_id="consumer",
            inputs=[FileArtifact(id="in_pdb", path=tmp_path / "c.txt")],
            outputs=[],
        )
        wf = HorusWorkflow(
            name="root_via_edges",
            tasks=[consumer],
            artifacts=[FileArtifact(id="root_pdb", path=root_path)],
            edges=[
                _edge("artifact-root_pdb", "root_pdb", "consumer", "in_pdb")
            ],
        )

        captured = _capture_transfer()
        with patch.object(
            BaseTransferStrategy,
            "get_from_registry",
            return_value=captured.strategy,
        ):
            await wf.transfer_artifacts(consumer)

        assert captured.source is wf.orchestrator_target
        assert captured.artifact_id == "root_pdb"
        assert consumer.inputs[0].path == root_path.resolve()


@pytest.mark.unit
class TestEdgeValidation:
    """Edges are validated against real endpoints at workflow construction."""

    def _producer_consumer(
        self, tmp_path: Path
    ) -> tuple[HorusTask, HorusTask]:
        producer = _task(
            task_id="producer",
            inputs=[],
            outputs=[FileArtifact(id="out_real", path=tmp_path / "p.txt")],
        )
        consumer = _task(
            task_id="consumer",
            inputs=[FileArtifact(id="in_x", path=tmp_path / "c.txt")],
            outputs=[],
        )
        return producer, consumer

    def test_unknown_source_output_raises(self, tmp_path: Path) -> None:
        """A typo in source_output is caught instead of silently misrouting."""
        producer, consumer = self._producer_consumer(tmp_path)
        with pytest.raises(UnknownEdgeEndpointError):
            HorusWorkflow(
                name="bad",
                tasks=[producer, consumer],
                edges=[_edge("producer", "TYPO", "consumer", "in_x")],
            )

    def test_unknown_source_task_raises(self, tmp_path: Path) -> None:
        """A source that is neither a known task nor a root edge fails."""
        producer, consumer = self._producer_consumer(tmp_path)
        with pytest.raises(UnknownEdgeEndpointError):
            HorusWorkflow(
                name="bad",
                tasks=[producer, consumer],
                edges=[_edge("producr", "out_real", "consumer", "in_x")],
            )

    def test_unknown_target_input_raises(self, tmp_path: Path) -> None:
        """A target_input that no task input declares is rejected."""
        producer, consumer = self._producer_consumer(tmp_path)
        with pytest.raises(UnknownEdgeEndpointError):
            HorusWorkflow(
                name="bad",
                tasks=[producer, consumer],
                edges=[_edge("producer", "out_real", "consumer", "in_TYPO")],
            )

    def test_unknown_target_task_raises(self, tmp_path: Path) -> None:
        """An edge targeting a non-existent task is rejected."""
        producer, consumer = self._producer_consumer(tmp_path)
        with pytest.raises(UnknownEdgeEndpointError):
            HorusWorkflow(
                name="bad",
                tasks=[producer, consumer],
                edges=[_edge("producer", "out_real", "ghost", "in_x")],
            )

    def test_unknown_root_artifact_raises(self, tmp_path: Path) -> None:
        """A root-sourced edge whose root id does not exist is rejected."""
        _, consumer = self._producer_consumer(tmp_path)
        with pytest.raises(UnknownEdgeEndpointError):
            HorusWorkflow(
                name="bad",
                tasks=[consumer],
                edges=[_edge("artifact-nope", "nope", "consumer", "in_x")],
            )

    def test_duplicate_edge_into_same_input_raises(
        self, tmp_path: Path
    ) -> None:
        """Two edges feeding one consumer input are rejected (last-wins)."""
        p1 = _task(
            task_id="p1",
            inputs=[],
            outputs=[FileArtifact(id="o1", path=tmp_path / "1.txt")],
        )
        p2 = _task(
            task_id="p2",
            inputs=[],
            outputs=[FileArtifact(id="o2", path=tmp_path / "2.txt")],
        )
        c = _task(
            task_id="c",
            inputs=[FileArtifact(id="in", path=tmp_path / "c.txt")],
            outputs=[],
        )
        with pytest.raises(DuplicateEdgeTargetError):
            HorusWorkflow(
                name="bad",
                tasks=[p1, p2, c],
                edges=[
                    _edge("p1", "o1", "c", "in"),
                    _edge("p2", "o2", "c", "in"),
                ],
            )


@pytest.mark.unit
class TestOrderingOnlyEdges:
    """
    ``transfer=False`` edges express dependency ordering without feeding a
    transfer source, so several may (eventually, for many-to-one fan-in) all
    order-gate the same consumer input.
    """

    def test_ordering_only_edge_resolves_and_orders(
        self, tmp_path: Path
    ) -> None:
        """A transfer=False edge still creates a DAG dependency."""
        producer = _task(
            task_id="producer",
            inputs=[],
            outputs=[FileArtifact(id="out", path=tmp_path / "p.txt")],
        )
        consumer = _task(
            task_id="consumer",
            inputs=[FileArtifact(id="in", path=tmp_path / "c.txt")],
            outputs=[],
        )
        edges = [_edge("producer", "out", "consumer", "in", transfer=False)]
        wf = HorusWorkflow(
            name="ordering_only", tasks=[producer, consumer], edges=edges
        )
        assert wf.edges[0].transfer is False

        plan = execution_plan(
            [consumer, producer], trigger_id="producer", edges=edges
        )
        assert plan == ["producer", "consumer"]

    def test_two_ordering_only_edges_into_same_input_allowed(
        self, tmp_path: Path
    ) -> None:
        """
        Two transfer=False edges may feed one input (no
        DuplicateEdgeTargetError), and neither contributes a transfer source.
        """
        p1 = _task(
            task_id="p1",
            inputs=[],
            outputs=[FileArtifact(id="o1", path=tmp_path / "1.txt")],
        )
        p2 = _task(
            task_id="p2",
            inputs=[],
            outputs=[FileArtifact(id="o2", path=tmp_path / "2.txt")],
        )
        c = _task(
            task_id="c",
            inputs=[FileArtifact(id="in", path=tmp_path / "c.txt")],
            outputs=[],
        )
        edges = [
            _edge("p1", "o1", "c", "in", transfer=False),
            _edge("p2", "o2", "c", "in", transfer=False),
        ]
        wf = HorusWorkflow(name="fan_in", tasks=[p1, p2, c], edges=edges)

        # Ordering still holds for both producers.
        deps = build_dependencies(wf.tasks, wf.edges)
        assert deps["c"] == {"p1", "p2"}

        # No transfer source is registered for the fed input.
        source_map = wf._build_source_map()
        assert ("c", "in") not in source_map

    def test_transfer_and_ordering_only_edge_coexist(
        self, tmp_path: Path
    ) -> None:
        """
        One transfer=True edge plus one transfer=False edge into the same
        input is allowed; the source map resolves only the transfer edge.
        """
        p1 = _task(
            task_id="p1",
            inputs=[],
            outputs=[FileArtifact(id="o1", path=tmp_path / "1.txt")],
        )
        p2 = _task(
            task_id="p2",
            inputs=[],
            outputs=[FileArtifact(id="o2", path=tmp_path / "2.txt")],
        )
        c = _task(
            task_id="c",
            inputs=[FileArtifact(id="in", path=tmp_path / "c.txt")],
            outputs=[],
        )
        edges = [
            _edge("p1", "o1", "c", "in", transfer=True),
            _edge("p2", "o2", "c", "in", transfer=False),
        ]
        wf = HorusWorkflow(name="mixed", tasks=[p1, p2, c], edges=edges)

        deps = build_dependencies(wf.tasks, wf.edges)
        assert deps["c"] == {"p1", "p2"}

        source_map = wf._build_source_map()
        source = source_map[("c", "in")]
        assert source.artifact is not None
        assert source.artifact.id == "o1"

    def test_two_transfer_edges_into_same_input_still_raises(
        self, tmp_path: Path
    ) -> None:
        """Two transfer=True edges into one input are still rejected."""
        p1 = _task(
            task_id="p1",
            inputs=[],
            outputs=[FileArtifact(id="o1", path=tmp_path / "1.txt")],
        )
        p2 = _task(
            task_id="p2",
            inputs=[],
            outputs=[FileArtifact(id="o2", path=tmp_path / "2.txt")],
        )
        c = _task(
            task_id="c",
            inputs=[FileArtifact(id="in", path=tmp_path / "c.txt")],
            outputs=[],
        )
        with pytest.raises(DuplicateEdgeTargetError):
            HorusWorkflow(
                name="bad",
                tasks=[p1, p2, c],
                edges=[
                    _edge("p1", "o1", "c", "in", transfer=True),
                    _edge("p2", "o2", "c", "in", transfer=True),
                ],
            )
