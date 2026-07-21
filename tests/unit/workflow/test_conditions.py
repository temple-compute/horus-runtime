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
Unit tests for conditional edges: the EdgeCondition / PythonCondition models,
their evaluation, and the liveness rule the scheduler gates on.
"""

import json
from pathlib import Path

import pytest
import yaml

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.condition import (
    ConditionEvaluationError,
    compute_liveness,
    evaluate_condition,
)
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.task.status import SkipReason, TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.core.workflow.condition import (
    EdgeCondition,
    PythonCondition,
)
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import IncompleteEdgeError


def _decider(
    tmp_path: Path, payload: object, *, task_id: str = "check"
) -> HorusTask:
    """A task whose JSON output already holds *payload*."""
    path = tmp_path / f"{task_id}.json"
    path.write_text(json.dumps(payload))
    return HorusTask(
        id=task_id,
        name=task_id,
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        outputs=[FileArtifact(id="decision", path=path)],
    )


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


def _gate(source: str, target: str, **condition: object) -> WorkflowEdge:
    """
    An artifact-less ordering edge from *source* to *target*, gated by a
    condition that names its own sentinel.

    A branch edge deliberately carries no data: the downstream task depends on
    the *decision*, not on the decider's output, so making it an ordering edge
    keeps the branch target free of an input it would otherwise have to declare
    and never receive.
    """
    return WorkflowEdge(
        source=source,
        target=target,
        condition=EdgeCondition(
            source_task="check",
            source_output="decision",
            **condition,  # type: ignore[arg-type]
        ),
    )


@pytest.mark.unit
class TestEdgeConditionModel:
    """Model-level validation of the declarative predicate."""

    def test_membership_ops_require_a_collection(self) -> None:
        """Membership operators need something to test membership against."""
        with pytest.raises(ValueError, match="membership"):
            EdgeCondition(key="x", op="in", value=3)

    def test_condition_needs_an_output_to_read(self) -> None:
        """
        An artifact-less ordering edge gives the condition no default source,
        so one must be named explicitly rather than failing mid-run.
        """
        with pytest.raises(IncompleteEdgeError):
            WorkflowEdge(
                source="a",
                target="b",
                condition=EdgeCondition(key="x", op="truthy"),
            )

    def test_condition_defaults_to_the_edges_own_endpoints(self) -> None:
        """An unset source falls back to the edge's own endpoints."""
        edge = WorkflowEdge(
            source="a",
            source_output="decision",
            target="b",
            target_input="signal",
            condition=EdgeCondition(key="branch", op="eq", value="x"),
        )
        assert edge.condition is not None
        assert edge.condition.source_task is None
        assert edge.condition.source_output is None

    def test_plain_edge_has_no_condition(self) -> None:
        """Every edge that predates branching keeps its old meaning."""
        assert WorkflowEdge(source="a", target="b").condition is None


@pytest.mark.unit
class TestEvaluateCondition:
    """Reading the sentinel and applying the operator."""

    async def _wf(self, tmp_path: Path, payload: object) -> BaseWorkflow:
        return HorusWorkflow(
            name="wf",
            tasks=[_decider(tmp_path, payload)],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )

    @pytest.mark.parametrize(
        ("op", "value", "expected"),
        [
            ("eq", "retrain", True),
            ("eq", "skip", False),
            ("ne", "skip", True),
            ("in", ["retrain", "other"], True),
            ("not_in", ["retrain"], False),
            ("truthy", None, True),
            ("exists", None, True),
        ],
    )
    async def test_operators(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        op: str,
        value: object,
        expected: bool,
    ) -> None:
        """Each operator in the closed set applies as expected."""
        del horus_context
        wf = await self._wf(tmp_path, {"branch": "retrain"})
        edge = _gate("check", "b", key="branch", op=op, value=value)
        assert await evaluate_condition(wf, edge) is expected

    async def test_dotted_key_walks_nested_documents(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A dotted key selects a nested field."""
        del horus_context
        wf = await self._wf(tmp_path, {"metrics": {"accuracy": 0.94}})
        edge = _gate("check", "b", key="metrics.accuracy", op="gt", value=0.9)
        assert await evaluate_condition(wf, edge) is True

    async def test_missing_key_is_absent_not_an_error(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A path that does not resolve reads as absent, so ``exists`` can
        distinguish "no such key" from "key is false".
        """
        del horus_context
        wf = await self._wf(tmp_path, {"branch": "retrain"})
        edge = _gate("check", "b", key="nope.deeper", op="exists")
        assert await evaluate_condition(wf, edge) is False

    async def test_unwritten_sentinel_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A predicate that cannot be read is a broken workflow, not a false
        branch: failing loudly beats a run that silently skips half the DAG.
        """
        del horus_context
        task = _decider(tmp_path, {"branch": "x"})
        (tmp_path / "check.json").unlink()
        wf = HorusWorkflow(
            name="wf",
            tasks=[task],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        edge = _gate("check", "b", key="branch", op="truthy")
        with pytest.raises(ConditionEvaluationError, match="never written"):
            await evaluate_condition(wf, edge)

    async def test_malformed_json_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A sentinel that is not JSON is a broken workflow."""
        del horus_context
        task = _decider(tmp_path, {"branch": "x"})
        (tmp_path / "check.json").write_text("{not json")
        wf = HorusWorkflow(
            name="wf",
            tasks=[task],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        edge = _gate("check", "b", key="branch", op="truthy")
        with pytest.raises(ConditionEvaluationError, match="not valid JSON"):
            await evaluate_condition(wf, edge)

    async def test_uncomparable_values_raise(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Ordering values that cannot be ordered is an error, not `false`."""
        del horus_context
        wf = await self._wf(tmp_path, {"branch": "text"})
        edge = _gate("check", "b", key="branch", op="gt", value=3)
        with pytest.raises(ConditionEvaluationError, match="Cannot compare"):
            await evaluate_condition(wf, edge)

    async def test_edge_without_condition_is_always_taken(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """An unconditioned edge keeps its pre-branching meaning."""
        del horus_context
        wf = await self._wf(tmp_path, {})
        edge = WorkflowEdge(source="check", target="b")
        assert await evaluate_condition(wf, edge) is True


@pytest.mark.unit
class TestLiveness:
    """The OR-join rule, evaluated directly."""

    async def test_root_task_is_live(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A task no other task feeds is always live."""
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            tasks=[_decider(tmp_path, {})],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        assert await compute_liveness(wf, "check", {}) is True

    async def test_dead_source_is_not_evaluated(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A dead task never wrote its sentinel, so a condition reading it would
        raise. Liveness must short-circuit on the dead source instead.
        """
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            tasks=[_decider(tmp_path, {}), _marker(tmp_path, "b")],
            edges=[_gate("check", "b", key="branch", op="truthy")],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        (tmp_path / "check.json").unlink()

        # No raise, despite the sentinel being absent, because `check` is
        # already known dead.
        assert await compute_liveness(wf, "b", {"check": False}) is False


@pytest.mark.unit
class TestDiamondBranchEndToEnd:
    """
    The shape that decides the whole design: ``A -> (B | C) -> D``.

    Blocking the untaken branch's descendants would also block D, because D is
    a descendant of C. Marking C skipped-but-completed is what lets the join
    run.
    """

    def _diamond(self, tmp_path: Path) -> HorusWorkflow:
        check = _decider(tmp_path, {"branch": "b"})
        join = HorusTask(
            id="d",
            name="d",
            runtime=CommandRuntime(
                command=f"touch {(tmp_path / 'd.done').as_posix()}"
            ),
            executor=ShellExecutor(),
            target=LocalTarget(),
            outputs=[FileArtifact(id="done", path=tmp_path / "d.done")],
            skip_if_complete=False,
        )
        return HorusWorkflow(
            name="wf",
            tasks=[
                check,
                _marker(tmp_path, "b"),
                _marker(tmp_path, "c"),
                join,
            ],
            edges=[
                _gate("check", "b", key="branch", op="eq", value="b"),
                _gate("check", "c", key="branch", op="eq", value="c"),
                WorkflowEdge(source="b", target="d"),
                WorkflowEdge(source="c", target="d"),
            ],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )

    async def test_join_runs_when_one_branch_is_skipped(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The join downstream of both branches still runs."""
        del horus_context
        wf = self._diamond(tmp_path)

        await wf.run(trigger_id="check")

        by_id = {t.id: t for t in wf.tasks}
        assert by_id["b"].status is TaskStatus.COMPLETED
        assert by_id["c"].status is TaskStatus.SKIPPED
        assert by_id["c"].skip_reason is SkipReason.INACTIVE

        # The whole point: the join is downstream of the skipped branch and
        # still runs.
        assert by_id["d"].status is TaskStatus.COMPLETED
        assert (tmp_path / "d.done").exists()

    async def test_untaken_branch_does_not_execute(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The dead branch's command never runs."""
        del horus_context
        wf = self._diamond(tmp_path)

        await wf.run(trigger_id="check")

        assert (tmp_path / "b.done").exists()
        assert not (tmp_path / "c.done").exists()

    async def test_run_ends_completed_not_failed(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A skipped branch must not look like a failure. Guards the path where
        the scheduler raises WorkflowExecutionError for anything in `failed`.
        """
        del horus_context
        wf = self._diamond(tmp_path)

        await wf.run(trigger_id="check")

        assert wf.status.value == "completed"


@pytest.mark.unit
class TestTransitiveDeactivation:
    """Deactivation propagates down a chain with no conditions of its own."""

    async def test_task_below_a_dead_branch_is_skipped(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Deactivation propagates to a task with no condition of its own."""
        del horus_context
        chained = _marker(tmp_path, "e")
        wf = HorusWorkflow(
            name="wf",
            tasks=[
                _decider(tmp_path, {"branch": "b"}),
                _marker(tmp_path, "c"),
                chained,
            ],
            edges=[
                _gate("check", "c", key="branch", op="eq", value="c"),
                WorkflowEdge(source="c", target="e"),
            ],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )

        await wf.run(trigger_id="check")

        by_id = {t.id: t for t in wf.tasks}
        assert by_id["c"].skip_reason is SkipReason.INACTIVE
        # `e` carries no condition at all; it is dead purely because its only
        # parent is.
        assert by_id["e"].status is TaskStatus.SKIPPED
        assert by_id["e"].skip_reason is SkipReason.INACTIVE
        assert not (tmp_path / "e.done").exists()


@pytest.mark.unit
class TestSkipReasonDistinguishesCacheHit:
    """A memoized skip and an untaken branch must not look alike."""

    async def test_memoized_skip_reports_complete(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A cache hit records COMPLETE, not INACTIVE."""
        del horus_context
        done = tmp_path / "cached.done"
        done.write_text("already here")
        task = HorusTask(
            id="cached",
            name="cached",
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            outputs=[FileArtifact(id="done", path=done)],
            skip_if_complete=True,
        )
        wf = HorusWorkflow(
            name="wf",
            tasks=[task],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )

        await wf.run(trigger_id="cached")

        assert task.status is TaskStatus.SKIPPED
        assert task.skip_reason is SkipReason.COMPLETE

    async def test_reset_clears_the_reason(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Resetting a task clears any recorded skip reason."""
        del horus_context
        task = _marker(tmp_path, "x")
        task.status = TaskStatus.SKIPPED
        task.skip_reason = SkipReason.INACTIVE

        await task.reset()

        assert task.status is TaskStatus.IDLE
        assert task.skip_reason is None


@pytest.mark.unit
class TestConditionYamlRoundTrip:
    """Conditions are a native edge key, so they survive a dump and reload."""

    async def test_declarative_condition_round_trips(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A declarative condition survives to_yaml then from_yaml."""
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            tasks=[
                _decider(tmp_path, {"branch": "b"}),
                _marker(tmp_path, "b"),
            ],
            edges=[_gate("check", "b", key="branch", op="eq", value="b")],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )

        path = tmp_path / "wf.yaml"
        wf.to_yaml(path)
        reloaded = BaseWorkflow.from_yaml(path)

        condition = reloaded.edges[0].condition
        assert isinstance(condition, EdgeCondition)
        assert (condition.key, condition.op, condition.value) == (
            "branch",
            "eq",
            "b",
        )

    async def test_condition_is_absent_from_plain_edges(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A workflow with no branching dumps a null condition and reloads."""
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            tasks=[_decider(tmp_path, {})],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        path = tmp_path / "wf.yaml"
        wf.to_yaml(path)

        raw = yaml.safe_load(path.read_text())
        assert raw["edges"] == []
        assert BaseWorkflow.from_yaml(path).edges == []


def _accuracy_is_high(document: object) -> bool:
    """Accuracy is above the 0.9 threshold."""
    assert isinstance(document, dict)
    return bool(document["metrics"]["accuracy"] > 0.9)


@pytest.mark.unit
class TestPythonCondition:
    """The Python authoring form, and the serialization contract behind it."""

    def test_ref_and_label_derive_from_the_callable(self) -> None:
        """Building from a function fills in ref and label."""
        condition = PythonCondition(func=_accuracy_is_high)
        assert condition.ref == f"{__name__}:_accuracy_is_high"
        assert condition.label == "Accuracy is above the 0.9 threshold."

    def test_dump_drops_the_callable_but_keeps_the_reference(self) -> None:
        """
        The contract the canvas depends on: a function cannot be serialized,
        so what reaches the UI is the reference and the label.
        """
        edge = WorkflowEdge(
            source="check",
            source_output="decision",
            target="b",
            target_input="signal",
            transfer=False,
            condition=PythonCondition(func=_accuracy_is_high),
        )
        dumped = edge.model_dump(mode="json")

        assert "func" not in dumped["condition"]
        assert dumped["condition"]["ref"] == f"{__name__}:_accuracy_is_high"
        assert dumped["condition"]["kind"] == "python"

    def test_reload_resolves_by_reference(self) -> None:
        """A reloaded condition keeps its ref but has no callable."""
        edge = WorkflowEdge(
            source="check",
            source_output="decision",
            target="b",
            target_input="signal",
            transfer=False,
            condition=PythonCondition(func=_accuracy_is_high),
        )
        reloaded = WorkflowEdge.model_validate(edge.model_dump(mode="json"))

        assert isinstance(reloaded.condition, PythonCondition)
        assert reloaded.condition.func is None
        assert reloaded.condition.ref == f"{__name__}:_accuracy_is_high"

    def test_lambda_gets_no_reference(self) -> None:
        """
        A lambda cannot be imported back, so it carries no ref: it works in
        process and fails loudly elsewhere, rather than emitting a reference
        that silently will not resolve.
        """
        assert PythonCondition(func=lambda _doc: True).ref is None

    def test_condition_without_callable_or_ref_is_rejected(self) -> None:
        """A condition that could never evaluate is rejected early."""
        with pytest.raises(ValueError, match="callable or a"):
            PythonCondition()

    async def test_unresolvable_ref_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Must not silently read as live."""
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            tasks=[_decider(tmp_path, {"metrics": {"accuracy": 0.95}})],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        edge = WorkflowEdge(
            source="check",
            source_output="decision",
            target="b",
            target_input="signal",
            transfer=False,
            condition=PythonCondition(ref="no.such.module:predicate"),
        )
        with pytest.raises(
            ConditionEvaluationError, match="cannot be imported"
        ):
            await evaluate_condition(wf, edge)

    async def test_callable_receives_the_source_document(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The predicate is handed the decoded sentinel."""
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            tasks=[_decider(tmp_path, {"metrics": {"accuracy": 0.95}})],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        edge = WorkflowEdge(
            source="check",
            source_output="decision",
            target="b",
            target_input="signal",
            transfer=False,
            condition=PythonCondition(func=_accuracy_is_high),
        )
        assert await evaluate_condition(wf, edge) is True

    async def test_python_branch_end_to_end(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A false Python predicate deactivates its branch in a real run."""
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            tasks=[
                _decider(tmp_path, {"metrics": {"accuracy": 0.5}}),
                _marker(tmp_path, "b"),
            ],
            edges=[
                WorkflowEdge(
                    source="check",
                    target="b",
                    condition=PythonCondition(
                        func=_accuracy_is_high,
                        source_task="check",
                        source_output="decision",
                    ),
                )
            ],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )

        await wf.run(trigger_id="check")

        by_id = {t.id: t for t in wf.tasks}
        assert by_id["b"].skip_reason is SkipReason.INACTIVE
        assert not (tmp_path / "b.done").exists()
