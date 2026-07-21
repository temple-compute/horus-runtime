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
Unit tests for BranchRouter: the switch-style authoring form, and the
property that keeps it honest — it lowers to the declarative conditions the
scheduler already understands, adding no second evaluation path.
"""

import json
from pathlib import Path

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.branch import (
    BranchConfigurationError,
    BranchRouter,
    route_edge,
)
from horus_builtin.workflow.condition import evaluate_condition
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.status import SkipReason, TaskStatus
from horus_runtime.core.workflow.condition import EdgeCondition
from horus_runtime.core.workflow.edge import WorkflowEdge


def _marker(tmp_path: Path, task_id: str) -> HorusTask:
    """A task that writes a file, so 'did it run?' is observable on disk."""
    out = tmp_path / f"{task_id}.done"
    return HorusTask(
        id=task_id,
        name=task_id,
        runtime=CommandRuntime(command=f"touch {out.as_posix()}"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        outputs=[FileArtifact(id="done", path=out)],
        skip_if_complete=False,
    )


def _workflow(tmp_path: Path, *tasks: BaseTask) -> HorusWorkflow:
    """A workflow rooted at *tmp_path*, holding *tasks*."""
    return HorusWorkflow(
        name="wf",
        tasks=list(tasks),
        orchestrator_target=LocalTarget(working_directory=tmp_path.as_posix()),
    )


def _pick_b() -> str:
    """Take the b branch."""
    return "b"


def _pick_both() -> list[str]:
    """Take both branches."""
    return ["b", "c"]


def _pick_none() -> list[str]:
    """Take no branch at all."""
    return []


def _pick_unknown() -> str:
    """Name a route that was never declared."""
    return "nowhere"


def _pick_nonsense() -> str:
    """Return something that is not a route id."""
    return 42  # type: ignore[return-value]


async def _pick_b_async() -> str:
    """Take the b branch, asynchronously."""
    return "b"


def _pick_own_id(task: BaseTask) -> str:
    """Decide using the injected router task."""
    assert isinstance(task, BranchRouter)
    return task.routes[0]


def _sentinel_of(router: BranchRouter) -> object:
    """The decoded JSON document *router* wrote."""
    artifact = next(
        a for a in router.outputs if a.id == router.routes_output_id
    )
    return json.loads(Path(router.target.path_on_target(artifact)).read_text())


@pytest.mark.unit
class TestBranchRouterModel:
    """Model-level behaviour: the sentinel output and the serializable ref."""

    def test_sentinel_output_is_added_automatically(self) -> None:
        """The router declares the output its edge conditions will read."""
        router = BranchRouter(id="r", name="r", func=_pick_b, routes=["b"])
        assert router.routes_output_id == "r.routes"
        assert [o.id for o in router.outputs] == ["r.routes"]

    def test_sentinel_output_is_not_duplicated_on_reload(self) -> None:
        """Re-validating an already-dumped router keeps one sentinel."""
        router = BranchRouter(id="r", name="r", func=_pick_b, routes=["b"])
        reloaded = BaseTask.model_validate(router.model_dump(mode="json"))
        assert [o.id for o in reloaded.outputs] == ["r.routes"]

    def test_ref_derives_from_the_callable(self) -> None:
        """Building from a function fills in the serializable reference."""
        router = BranchRouter(id="r", name="r", func=_pick_b, routes=["b"])
        assert router.ref == f"{__name__}:_pick_b"

    def test_lambda_gets_no_reference(self) -> None:
        """
        A lambda cannot be imported back, so it carries no ref — the same
        contract PythonCondition makes, via the same shared helper.
        """
        router = BranchRouter(id="r", name="r", func=lambda: "b", routes=["b"])
        assert router.ref is None

    def test_dump_drops_the_callable_but_keeps_the_reference(self) -> None:
        """A function cannot be serialized; what survives is the reference."""
        router = BranchRouter(id="r", name="r", func=_pick_b, routes=["b"])
        dumped = router.model_dump(mode="json")

        assert "func" not in dumped
        assert dumped["ref"] == f"{__name__}:_pick_b"
        assert dumped["kind"] == "branch_router"
        assert dumped["routes"] == ["b"]

    def test_router_without_callable_or_ref_is_rejected(self) -> None:
        """A router that could never decide is rejected early."""
        with pytest.raises(ValueError, match="callable or a"):
            BranchRouter(id="r", name="r", routes=["b"])

    async def test_is_never_complete(self) -> None:
        """
        The router must re-decide every run: a stale sentinel would pin a
        resumed run to the branch it took last time.
        """
        router = BranchRouter(id="r", name="r", func=_pick_b, routes=["b"])
        assert await router.is_complete() is False


@pytest.mark.unit
class TestLowersToDeclarative:
    """
    The property the whole design rests on: a router produces exactly the
    wiring a hand-written declarative branch would, so there is only ever one
    evaluation path.
    """

    def test_generated_edges_match_a_hand_written_branch(
        self, tmp_path: Path
    ) -> None:
        """Router-generated edges are identical to hand-written ones."""
        wf = _workflow(
            tmp_path, _marker(tmp_path, "b"), _marker(tmp_path, "c")
        )
        wf.branch(id="r", func=_pick_b, routes=["b", "c"])

        hand_written = [
            WorkflowEdge(
                source="r",
                target=route,
                condition=EdgeCondition(
                    source_task="r",
                    source_output="r.routes",
                    key="routes",
                    op="contains",
                    value=route,
                ),
            )
            for route in ("b", "c")
        ]

        assert [e.model_dump(mode="json") for e in wf.edges] == [
            e.model_dump(mode="json") for e in hand_written
        ]

    async def test_sentinel_is_plain_data_a_hand_written_edge_can_read(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The sentinel carries no router-specific structure: an edge authored
        by hand, with no knowledge of routers, evaluates it correctly.
        """
        del horus_context
        wf = _workflow(
            tmp_path, _marker(tmp_path, "b"), _marker(tmp_path, "c")
        )
        router = wf.branch(id="r", func=_pick_b, routes=["b", "c"])

        await wf.run(trigger_id="r")

        assert _sentinel_of(router) == {"routes": ["b"]}

        taken = WorkflowEdge(
            source="r",
            target="b",
            condition=EdgeCondition(
                source_task="r",
                source_output="r.routes",
                key="routes",
                op="contains",
                value="b",
            ),
        )
        untaken = WorkflowEdge(
            source="r",
            target="c",
            condition=EdgeCondition(
                source_task="r",
                source_output="r.routes",
                key="routes",
                op="contains",
                value="c",
            ),
        )
        assert await evaluate_condition(wf, taken) is True
        assert await evaluate_condition(wf, untaken) is False

    async def test_dumped_router_edges_survive_a_yaml_round_trip(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Nothing about the branch needs the callable to round-trip: the edges
        are ordinary declarative conditions.
        """
        del horus_context
        wf = _workflow(
            tmp_path, _marker(tmp_path, "b"), _marker(tmp_path, "c")
        )
        wf.branch(id="r", func=_pick_b, routes=["b", "c"])

        path = tmp_path / "wf.yaml"
        wf.to_yaml(path)
        reloaded = HorusWorkflow.from_yaml(path)

        condition = reloaded.edges[0].condition
        assert isinstance(condition, EdgeCondition)
        assert (condition.key, condition.op, condition.value) == (
            "routes",
            "contains",
            "b",
        )


@pytest.mark.unit
class TestDecision:
    """Calling the function, and validating what it returns."""

    async def _route(
        self, tmp_path: Path, func: object, routes: list[str]
    ) -> BranchRouter:
        wf = _workflow(
            tmp_path, _marker(tmp_path, "b"), _marker(tmp_path, "c")
        )
        router = wf.branch(
            id="r",
            func=func,  # type: ignore[arg-type]
            routes=routes,
        )
        await wf.run(trigger_id="r")
        return router

    async def test_single_id_is_accepted(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Returning a bare route id takes that one branch."""
        del horus_context
        router = await self._route(tmp_path, _pick_b, ["b", "c"])
        assert _sentinel_of(router) == {"routes": ["b"]}

    async def test_list_of_ids_is_accepted(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A router may fan out to several branches at once."""
        del horus_context
        router = await self._route(tmp_path, _pick_both, ["b", "c"])
        assert _sentinel_of(router) == {"routes": ["b", "c"]}

    async def test_empty_list_takes_no_branch(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Choosing nothing is a legal decision, not an error."""
        del horus_context
        router = await self._route(tmp_path, _pick_none, ["b", "c"])
        assert _sentinel_of(router) == {"routes": []}

    async def test_async_function_is_awaited(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A decision may need to read something remote."""
        del horus_context
        router = await self._route(tmp_path, _pick_b_async, ["b", "c"])
        assert _sentinel_of(router) == {"routes": ["b"]}

    async def test_task_parameter_is_injected(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A function asking for `task` receives the router itself."""
        del horus_context
        router = await self._route(tmp_path, _pick_own_id, ["b", "c"])
        assert _sentinel_of(router) == {"routes": ["b"]}

    async def test_undeclared_route_is_rejected(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A typo'd route id must fail loudly: silently taking no branch would
        look like a deliberate decision and skip the rest of the DAG.
        """
        del horus_context
        with pytest.raises(BranchConfigurationError, match="'nowhere'"):
            await self._route(tmp_path, _pick_unknown, ["b", "c"])

    async def test_non_string_return_is_rejected(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A function returning something that is not a route id is a bug."""
        del horus_context
        with pytest.raises(BranchConfigurationError, match="must return"):
            await self._route(tmp_path, _pick_nonsense, ["b", "c"])

    async def test_undeclared_route_names_the_declared_set(
        self, tmp_path: Path
    ) -> None:
        """The error says what was chosen and what was available."""
        router = BranchRouter(
            id="r", name="r", func=_pick_unknown, routes=["b", "c"]
        )
        del tmp_path
        with pytest.raises(BranchConfigurationError, match="'nowhere'"):
            await router._run()


@pytest.mark.unit
class TestBranchBuilder:
    """`wf.branch(...)` wiring and its construction-time guards."""

    def test_router_and_one_edge_per_route_are_appended(
        self, tmp_path: Path
    ) -> None:
        """One call produces the router plus its whole fan-out."""
        wf = _workflow(
            tmp_path, _marker(tmp_path, "b"), _marker(tmp_path, "c")
        )
        router = wf.branch(id="r", func=_pick_b, routes=["b", "c"])

        assert wf.tasks[-1] is router
        assert [(e.source, e.target) for e in wf.edges] == [
            ("r", "b"),
            ("r", "c"),
        ]

    def test_edges_are_ordering_only(self, tmp_path: Path) -> None:
        """
        A branch target depends on the decision, not on the sentinel's bytes,
        so it needs no input to receive them into.
        """
        wf = _workflow(tmp_path, _marker(tmp_path, "b"))
        wf.branch(id="r", func=_pick_b, routes=["b"])

        edge = wf.edges[0]
        assert (edge.source_output, edge.target_input) == (None, None)
        assert edge.transfer is False

    def test_unknown_route_is_rejected(self, tmp_path: Path) -> None:
        """Routing to a task that does not exist is caught at build time."""
        wf = _workflow(tmp_path, _marker(tmp_path, "b"))
        with pytest.raises(BranchConfigurationError, match="unknown task"):
            wf.branch(id="r", func=_pick_b, routes=["b", "ghost"])

    def test_empty_routes_are_rejected(self, tmp_path: Path) -> None:
        """A branch with nowhere to go is a mistake."""
        wf = _workflow(tmp_path, _marker(tmp_path, "b"))
        with pytest.raises(BranchConfigurationError, match="at least one"):
            wf.branch(id="r", func=_pick_b, routes=[])

    def test_duplicate_routes_are_rejected(self, tmp_path: Path) -> None:
        """Two identical edges to one target would just be noise."""
        wf = _workflow(tmp_path, _marker(tmp_path, "b"))
        with pytest.raises(BranchConfigurationError, match="duplicate"):
            wf.branch(id="r", func=_pick_b, routes=["b", "b"])

    def test_route_edge_is_the_single_definition_of_the_wiring(self) -> None:
        """The builder emits exactly what `route_edge` describes."""
        edge = route_edge("r", "r.routes", "b")
        assert isinstance(edge.condition, EdgeCondition)
        assert edge.condition.source_task == "r"
        assert edge.condition.source_output == "r.routes"


@pytest.mark.unit
class TestBranchEndToEnd:
    """A real run takes only the chosen branch, and ends completed."""

    def _wf(self, tmp_path: Path) -> HorusWorkflow:
        wf = _workflow(
            tmp_path, _marker(tmp_path, "b"), _marker(tmp_path, "c")
        )
        wf.branch(id="r", func=_pick_b, routes=["b", "c"])
        return wf

    async def test_only_the_chosen_branch_runs(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The untaken branch's command never executes."""
        del horus_context
        wf = self._wf(tmp_path)

        await wf.run(trigger_id="r")

        assert (tmp_path / "b.done").exists()
        assert not (tmp_path / "c.done").exists()

    async def test_untaken_branch_is_skipped_as_inactive(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The router reuses the scheduler's existing liveness gate."""
        del horus_context
        wf = self._wf(tmp_path)

        await wf.run(trigger_id="r")

        by_id = {t.id: t for t in wf.tasks}
        assert by_id["b"].status is TaskStatus.COMPLETED
        assert by_id["c"].status is TaskStatus.SKIPPED
        assert by_id["c"].skip_reason is SkipReason.INACTIVE

    async def test_run_ends_completed_not_failed(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """An untaken branch must not look like a failure."""
        del horus_context
        wf = self._wf(tmp_path)

        await wf.run(trigger_id="r")

        assert wf.status.value == "completed"

    async def test_multi_route_branch_runs_every_chosen_arm(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Returning several ids activates all of them."""
        del horus_context
        wf = _workflow(
            tmp_path, _marker(tmp_path, "b"), _marker(tmp_path, "c")
        )
        wf.branch(id="r", func=_pick_both, routes=["b", "c"])

        await wf.run(trigger_id="r")

        assert (tmp_path / "b.done").exists()
        assert (tmp_path / "c.done").exists()
