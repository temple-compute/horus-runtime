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
Tests for the lease manager: when a releasable (shared) machine is held,
drained and released during a run.
"""

import asyncio
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from pydantic import PrivateAttr

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_builtin.workflow.map import MapOver, MapTask
from horus_runtime.context import HorusContext
from horus_runtime.core import lease as lease_module
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.lease import BaseEvacuator, OrchestratorEvacuator
from horus_runtime.core.placement import PlacementManager, ResourceCapacity
from horus_runtime.core.resources import ResourceRequest
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.workflow.base import EdgeSource
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import (
    ArtifactLostError,
    WorkflowExecutionError,
)


class FakeNode(LocalTarget):
    """
    A releasable machine on the local filesystem. Its node state lives in
    one dict shared by every ``idle_copy``, like a real shared node handle.
    Binding a task provisions it (a new generation) when it is not live.
    """

    kind: str = "fake_node"
    releasable: ClassVar[bool] = True
    hold_s: float = 30.0
    parallel: int | None = None
    capacity: ResourceCapacity | None = None

    _node: dict[str, Any] = PrivateAttr(
        default_factory=lambda: {
            "live": False,
            "gen": 0,
            "released": [],
            "phases": [],
            "keepalives": 0,
        }
    )

    @property
    def hold_timeout_s(self) -> float:
        """Seconds the machine holds."""
        return self.hold_s

    @property
    def max_parallel(self) -> int | None:
        """Member cap."""
        return self.parallel

    @property
    def is_live(self) -> bool:
        """Whether the fake node exists."""
        return bool(self._node["live"])

    @property
    def generation(self) -> int:
        """Provision count."""
        return int(self._node["gen"])

    @property
    def released(self) -> list[str]:
        """Release reasons, in order."""
        return cast(list[str], self._node["released"])

    @property
    def phases(self) -> list[str]:
        """Emitted lease phases, in order."""
        return cast(list[str], self._node["phases"])

    def node_capacity(self) -> ResourceCapacity | None:
        """The declared capacity."""
        return self.capacity

    def bind(self, task: Any) -> None:
        """Provision on first bind."""
        super().bind(task)
        if not self._node["live"]:
            self._node["live"] = True
            self._node["gen"] += 1

    async def release(self, reason: str) -> None:
        """Record the release."""
        if self._node["live"]:
            self._node["released"].append(reason)
            self._node["live"] = False

    async def keepalive(self) -> None:
        """Count the ping."""
        self._node["keepalives"] += 1

    def emit_node_phase(self, phase: str, **fields: Any) -> None:
        """Record the phase."""
        del fields
        self._node["phases"].append(phase)


class _Step(HorusTask):
    """A task that waits *delay* seconds, then succeeds or fails."""

    add_to_registry: ClassVar[bool] = False
    delay: float = 0.0
    fail: bool = False

    async def _run(self) -> None:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError(f"{self.id} failed")
        self.runs += 1


def _step(
    task_id: str,
    target: BaseTarget,
    tmp_path: Path,
    *,
    delay: float = 0.0,
    fail: bool = False,
    inputs: list[str] = (),  # type: ignore[assignment]
) -> _Step:
    return _Step(
        id=task_id,
        name=task_id,
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=target,
        inputs=cast(
            list[BaseArtifact],
            [FileArtifact(id=i, path=tmp_path / f"{i}.in") for i in inputs],
        ),
        outputs=cast(
            list[BaseArtifact],
            [FileArtifact(id="out", path=tmp_path / f"{task_id}.out")],
        ),
        delay=delay,
        fail=fail,
    )


def _edge(source: str, target: str, target_input: str) -> WorkflowEdge:
    return WorkflowEdge(
        source=source,
        source_output="out",
        target=target,
        target_input=target_input,
    )


def _workflow(
    tmp_path: Path,
    tasks: list[HorusTask],
    edges: list[WorkflowEdge],
    **kwargs: Any,
) -> HorusWorkflow:
    return HorusWorkflow(
        name="wf",
        tasks=cast(list[Any], tasks),
        edges=edges,
        orchestrator_target=LocalTarget(working_directory=tmp_path.as_posix()),
        **kwargs,
    )


class _RecordingEvacuator(BaseEvacuator):
    """Relocates every output to the orchestrator without moving bytes."""

    add_to_registry: ClassVar[bool] = False
    kind: str = "recording"
    calls: ClassVar[list[tuple[str, str]]] = []
    fail: ClassVar[bool] = False
    delay: ClassVar[float] = 0.0

    async def evacuate(
        self, task: Any, output: BaseArtifact, source: BaseTarget
    ) -> EdgeSource:
        del source
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("upload failed")
        self.calls.append((task.id, output.id))
        return EdgeSource(LocalTarget(), output, task.id)


@pytest.fixture
def evacuator(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingEvacuator]:
    """Evacuator."""
    _RecordingEvacuator.calls = []
    _RecordingEvacuator.fail = False
    _RecordingEvacuator.delay = 0.0
    monkeypatch.setattr(lease_module, "_pick_evacuator", _RecordingEvacuator)
    return _RecordingEvacuator


@pytest.mark.unit
class TestRelease:
    """When a machine is released, and why."""

    async def test_released_once_after_the_last_member(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """Two chained members: one machine, released once, at the end."""
        del horus_context
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path)
        b = _step("b", node, tmp_path, inputs=["x"])
        wf = _workflow(tmp_path, [a, b], [_edge("a", "b", "x")])

        await wf.run(trigger_id="a")

        assert node.released == ["done"]
        assert node.generation == 1
        assert b.runs == 1
        # A same-machine consumer pulls nothing off it, so nothing moved.
        assert evacuator.calls == []

    async def test_outside_consumer_drains_before_release(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """
        Once only another machine's task still needs an output, the output
        is evacuated and relocated, then the machine is released.
        """
        del horus_context
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path)
        c = _step("c", LocalTarget(), tmp_path, inputs=["x"])
        wf = _workflow(tmp_path, [a, c], [_edge("a", "c", "x")])

        await wf.run(trigger_id="a")

        assert node.released == ["drained"]
        assert evacuator.calls == [("a", "out")]
        assert "draining" in node.phases
        assert c.runs == 1

    async def test_consumer_that_already_pulled_holds_nothing(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """
        An outside consumer stops holding the machine once its inputs are
        transferred, so the release after the last member evacuates nothing.
        A member dispatched right after another one never shows as holding.
        """
        del horus_context
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path)
        b = _step("b", node, tmp_path, delay=0.2, inputs=["x"])
        c = _step("c", LocalTarget(), tmp_path, inputs=["x"])
        wf = _workflow(
            tmp_path,
            [a, b, c],
            [_edge("a", "b", "x"), _edge("a", "c", "x")],
        )

        await wf.run(trigger_id="a")

        assert node.released == ["done"]
        assert evacuator.calls == []
        assert "holding" not in node.phases

    async def test_member_starting_during_a_hold_cancels_it(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """A gap shorter than the hold keeps the same machine."""
        del horus_context, evacuator
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path)
        g = _step("g", LocalTarget(), tmp_path, delay=0.1)
        b = _step("b", node, tmp_path, inputs=["y"])
        wf = _workflow(tmp_path, [a, g, b], [_edge("g", "b", "y")])
        wf.edges.append(WorkflowEdge(source="a", target="g"))

        await wf.run(trigger_id="a")

        assert node.phases == ["holding", "busy"]
        assert node.released == ["done"]
        assert node.generation == 1

    async def test_hold_expiry_drains_and_reprovisions(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """
        A member waiting on a slow task elsewhere outlasts the hold: the
        machine is drained and released, and the next member provisions a
        second generation, reading its input from the relocated source.
        """
        del horus_context
        node = FakeNode(working_directory=tmp_path.as_posix(), hold_s=0.05)
        a = _step("a", node, tmp_path)
        g = _step("g", LocalTarget(), tmp_path, delay=0.3)
        b = _step("b", node, tmp_path, inputs=["x", "y"])
        wf = _workflow(
            tmp_path,
            [a, g, b],
            [_edge("a", "b", "x"), _edge("g", "b", "y")],
        )

        await wf.run(trigger_id="a")

        assert node.released == ["idle_cap", "done"]
        assert evacuator.calls == [("a", "out")]
        assert node.generation == 2
        assert b.runs == 1

    async def test_zero_hold_releases_at_the_first_gap(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """``hold_timeout`` 0 releases as soon as no member runs."""
        del horus_context, evacuator
        node = FakeNode(working_directory=tmp_path.as_posix(), hold_s=0)
        a = _step("a", node, tmp_path)
        g = _step("g", LocalTarget(), tmp_path, delay=0.1)
        b = _step("b", node, tmp_path, inputs=["y"])
        wf = _workflow(tmp_path, [a, g, b], [_edge("g", "b", "y")])
        wf.edges.append(WorkflowEdge(source="a", target="g"))

        await wf.run(trigger_id="a")

        assert node.released == ["idle_cap", "done"]
        assert node.generation == 2

    async def test_member_starting_mid_drain_keeps_the_machine(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """
        A member that starts while the machine drains is not destroyed
        under: the release is called off once the drain finishes.
        """
        del horus_context
        evacuator.delay = 0.3
        node = FakeNode(working_directory=tmp_path.as_posix(), hold_s=0)
        a = _step("a", node, tmp_path)
        g = _step("g", LocalTarget(), tmp_path, delay=0.05)
        b = _step("b", node, tmp_path, delay=0.4, inputs=["x", "y"])
        wf = _workflow(
            tmp_path,
            [a, g, b],
            [_edge("a", "b", "x"), _edge("g", "b", "y")],
        )

        await wf.run(trigger_id="a")

        assert evacuator.calls == [("a", "out")]
        assert node.released == ["done"]
        assert node.generation == 1
        assert b.runs == 1

    async def test_pending_subworkflow_holds_its_body_machines(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """
        A subworkflow not inlined yet counts as a member of the machines its
        body uses, so the producer it feeds is not released under it.
        """
        del horus_context, evacuator
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path)
        g = _step("g", LocalTarget(), tmp_path, delay=0.2)
        inner = _step("x", node, tmp_path, inputs=["seed"])
        child = HorusWorkflow(
            name="child",
            tasks=[inner],
            artifacts=[FileArtifact(id="seed", path=tmp_path / "seed")],
            edges=[
                WorkflowEdge(
                    source="artifact-seed",
                    source_output="seed",
                    target="x",
                    target_input="seed",
                )
            ],
        )
        wf = _workflow(tmp_path, [a, g], [], targets={"box": node})
        wf.subworkflow(id="sub", body=child)
        wf.edges += [
            WorkflowEdge(
                source="a",
                source_output="out",
                target="sub",
                target_input="seed",
                transfer=False,
            ),
            WorkflowEdge(source="g", target="sub"),
        ]
        start = _step("start", LocalTarget(), tmp_path)
        wf.tasks.append(start)
        wf.edges += [
            WorkflowEdge(source="start", target="a"),
            WorkflowEdge(source="start", target="g"),
        ]

        await wf.run(trigger_id="start")

        assert wf.status.value == "completed"
        assert node.released == ["done"]
        assert node.generation == 1

    async def test_failed_evacuation_keeps_the_machine(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """
        Data a pending task needs is never destroyed: a failed drain keeps
        the machine until run end, and the consumer pulls from it directly.
        """
        del horus_context
        evacuator.fail = True
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path)
        c = _step("c", LocalTarget(), tmp_path, inputs=["x"])
        wf = _workflow(tmp_path, [a, c], [_edge("a", "c", "x")])

        await wf.run(trigger_id="a")

        assert "drain_failed" in node.phases
        assert node.released == ["run_end"]
        assert c.runs == 1

    async def test_blocked_members_release_the_machine(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """
        Under ``continue``, members blocked behind a failure stop holding
        the machine: it is released then, not at run end.
        """
        del horus_context, evacuator
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path)
        f = _step("f", LocalTarget(), tmp_path, delay=0.1, fail=True)
        b = _step("b", node, tmp_path, inputs=["y"])
        start = _step("start", LocalTarget(), tmp_path)
        wf = _workflow(
            tmp_path,
            [start, a, f, b],
            [_edge("f", "b", "y")],
            failure_policy="continue",
        )
        wf.edges += [
            WorkflowEdge(source="start", target="a"),
            WorkflowEdge(source="start", target="f"),
        ]

        with pytest.raises(WorkflowExecutionError):
            await wf.run(trigger_id="start")

        assert node.released == ["done"]
        assert b.runs == 0

    async def test_fail_fast_aborts_without_evacuation(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """A fail-fast abort releases live machines and moves nothing."""
        del horus_context
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path, delay=5)
        f = _step("f", LocalTarget(), tmp_path, delay=0.05, fail=True)
        c = _step("c", LocalTarget(), tmp_path, inputs=["x"])
        start = _step("start", LocalTarget(), tmp_path)
        wf = _workflow(tmp_path, [start, a, f, c], [_edge("a", "c", "x")])
        wf.edges += [
            WorkflowEdge(source="start", target="a"),
            WorkflowEdge(source="start", target="f"),
        ]

        with pytest.raises(RuntimeError, match="f failed"):
            await wf.run(trigger_id="start")

        assert node.released == ["abort"]
        assert evacuator.calls == []

    async def test_member_added_mid_run_keeps_the_machine(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """
        A member that expands the DAG with another member before finishing
        must not see its machine released early (stale scope).
        """
        del horus_context, evacuator
        node = FakeNode(working_directory=tmp_path.as_posix())
        late = _step("late", node, tmp_path)

        class _Expands(_Step):
            async def _run(self) -> None:
                workflow = HorusContext.get_context().workflow
                assert workflow is not None
                workflow.add_task(late)
                await super()._run()

        a = _Expands(
            id="a",
            name="a",
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
            target=node,
        )
        wf = _workflow(tmp_path, [a], [])

        await wf.run(trigger_id="a")

        assert late.runs == 1
        assert node.released == ["done"]
        assert node.generation == 1

    async def test_keepalive_pings_a_live_machine(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The manager pings live machines while the run needs them."""
        del horus_context
        monkeypatch.setattr(
            lease_module.LeaseManager, "keepalive_interval_s", 0.01
        )
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path, delay=0.1)
        wf = _workflow(tmp_path, [a], [])

        await wf.run(trigger_id="a")

        assert node._node["keepalives"] > 0
        assert node.released == ["done"]

    async def test_non_releasable_targets_get_no_lease(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """An ordinary target is untouched by the lease manager."""
        del horus_context
        a = _step("a", LocalTarget(), tmp_path)
        wf = _workflow(tmp_path, [a], [])

        await wf.run(trigger_id="a")

        assert not wf.leases.has_leases


@pytest.mark.unit
class TestSlotsAndCapacity:
    """``max_parallel`` and the machine's declared capacity."""

    async def test_max_parallel_serializes_members(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """At most ``max_parallel`` members run at once."""
        del horus_context, evacuator
        node = FakeNode(working_directory=tmp_path.as_posix(), parallel=1)
        running: list[int] = [0]
        peak: list[int] = [0]

        class _Counts(_Step):
            async def _run(self) -> None:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
                await asyncio.sleep(0.05)
                running[0] -= 1

        tasks = [
            _Counts(
                id=f"t{i}",
                name=f"t{i}",
                runtime=CommandRuntime(command="true"),
                executor=ShellExecutor(),
                target=node,
            )
            for i in range(3)
        ]
        start = _step("start", LocalTarget(), tmp_path)
        wf = _workflow(
            tmp_path,
            [start, *tasks],
            [WorkflowEdge(source="start", target=t.id) for t in tasks],
        )

        await wf.run(trigger_id="start")

        assert peak[0] == 1
        assert node.released == ["done"]

    async def test_machine_capacity_gates_members_with_resources(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """
        A known machine size is declared to placement, so members that
        declare resources never oversubscribe it.
        """
        del horus_context, evacuator
        node = FakeNode(
            working_directory=tmp_path.as_posix(),
            capacity=ResourceCapacity(cpus=4),
        )
        running: list[int] = [0]
        peak: list[int] = [0]

        class _Counts(_Step):
            async def _run(self) -> None:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
                await asyncio.sleep(0.05)
                running[0] -= 1

        tasks = [
            _Counts(
                id=f"t{i}",
                name=f"t{i}",
                runtime=CommandRuntime(command="true"),
                executor=ShellExecutor(),
                target=node,
                resources=ResourceRequest(cpus=4),
            )
            for i in range(3)
        ]
        start = _step("start", LocalTarget(), tmp_path)
        wf = _workflow(
            tmp_path,
            [start, *tasks],
            [WorkflowEdge(source="start", target=t.id) for t in tasks],
        )

        await wf.run(trigger_id="start")

        assert peak[0] == 1

    async def test_declare_keeps_an_existing_capacity(self) -> None:
        """``declare`` never overrides capacity the workflow declared."""
        placement = PlacementManager({"loc": ResourceCapacity(cpus=2)})
        placement.declare("loc", ResourceCapacity(cpus=64))
        placement.declare("new", ResourceCapacity(cpus=8))

        await placement.acquire("t", "loc", ResourceRequest(cpus=2))
        assert not placement._fits("loc", {"cpus": 1})
        assert placement._fits("new", {"cpus": 8})


@pytest.mark.unit
class TestLostArtifacts:
    """Reading an output whose machine generation is gone."""

    async def test_transfer_from_a_released_generation_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """No silent zero-copy read off a re-provisioned machine."""
        del horus_context
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path)
        b = _step("b", node, tmp_path, inputs=["x"])
        wf = _workflow(tmp_path, [a, b], [_edge("a", "b", "x")])
        wf.leases._produced[("a", "out")] = 1
        node._node.update(live=True, gen=2)

        with pytest.raises(ArtifactLostError, match="gen 1"):
            await wf.transfer_artifacts(b)

    async def test_stale_source_map_follows_a_relocation(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A task dispatched before its input was evacuated still reads the
        relocated copy, not the released machine.
        """
        del horus_context
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path)
        b = _step("b", LocalTarget(), tmp_path, inputs=["x"])
        wf = _workflow(tmp_path, [a, b], [_edge("a", "b", "x")])
        stale = wf.cached_source_map(None)[1]
        wf.leases._produced[("a", "out")] = 1
        node._node.update(live=False, gen=1)
        wf.relocate_output(
            "a", "out", EdgeSource(LocalTarget(), a.outputs[0], "a")
        )

        await wf.transfer_artifacts(b, stale)

    async def test_transfer_from_the_live_generation_is_fine(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The generation an output was produced on is still there."""
        del horus_context
        node = FakeNode(working_directory=tmp_path.as_posix())
        a = _step("a", node, tmp_path)
        b = _step("b", node, tmp_path, inputs=["x"])
        wf = _workflow(tmp_path, [a, b], [_edge("a", "b", "x")])
        wf.leases._produced[("a", "out")] = 1
        node._node.update(live=True, gen=1)

        await wf.transfer_artifacts(b)


@pytest.mark.unit
class TestOrchestratorEvacuator:
    """The default evacuator copies an output to the orchestrator."""

    async def test_copies_into_a_per_task_directory(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The copy lands under ``.evacuated/<task>/`` and is the source."""
        source_dir = tmp_path / "node"
        source_dir.mkdir()
        (source_dir / "a.out").write_text("payload")

        class _Elsewhere(FakeNode):
            add_to_registry: ClassVar[bool] = False

            @property
            def location_id(self) -> str:
                return f"fake://{self.lease_key}"

        node = _Elsewhere(working_directory=source_dir.as_posix())
        a = _step("a", node, source_dir)
        orchestrator = tmp_path / "orch"
        orchestrator.mkdir()
        wf = _workflow(orchestrator, [a], [])
        horus_context.workflow = wf

        source = await OrchestratorEvacuator().evacuate(a, a.outputs[0], node)

        assert source.task_id == "a"
        assert source.target is not None
        assert source.artifact is not None
        copied = orchestrator / ".evacuated" / "a" / "a.out"
        assert Path(source.artifact.path) == copied
        assert copied.read_text() == "payload"


@pytest.mark.unit
class TestMapOnSharedMachine:
    """A map whose clones share the map's machine."""

    async def test_clones_share_one_lease_without_deadlock(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        evacuator: type[_RecordingEvacuator],
    ) -> None:
        """
        Three clones under ``max_parallel=1``: the waiting map parent takes
        no slot, every clone runs on the one machine, released once.
        """
        del horus_context, evacuator
        batches = tmp_path / "batches"
        batches.mkdir()
        for name in ("a", "b", "c"):
            (batches / name).write_text(name)
        split = HorusTask(
            id="split",
            name="split",
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            outputs=[FolderArtifact(id="batches", path=batches)],
        )
        node = FakeNode(working_directory=tmp_path.as_posix(), parallel=1)
        score = MapTask(
            id="score",
            name="score",
            over=MapOver(input_id="batches", item_id="batch"),
            runtime=CommandRuntime(command="cp $batch $scored/result.txt"),
            executor=ShellExecutor(),
            target=node,
            inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
            outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
        )
        wf = _workflow(
            tmp_path,
            [split, score],
            [
                WorkflowEdge(
                    source="split",
                    source_output="batches",
                    target="score",
                    target_input="batches",
                )
            ],
        )

        await asyncio.wait_for(wf.run(trigger_id="split"), timeout=10)

        assert wf.status.value == "completed"
        assert node.released == ["done"]
        assert node.generation == 1
        assert len(wf.leases._leases) == 0  # cleared at run end
