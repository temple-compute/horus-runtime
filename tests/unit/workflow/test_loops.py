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
Unit tests for the conditional-repeat loop construct: LoopController, the
``loop:`` YAML lowering hook, and the ``wf.loop(...)`` Python builder.

The bounded/counted loop ("run exactly N times") is covered by the range
map in ``test_map.py``; :class:`TestBoundedLoopViaRangeMap` re-asserts that
acceptance here so #116's "bounded loop" criterion is visibly met alongside
the conditional loop.
"""

from pathlib import Path

import pytest
import yaml

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_builtin.workflow.loop import (
    LoopConfigurationError,
    LoopController,
    lower_loop_entry,
)
from horus_runtime.context import HorusContext
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow

# A tiny shell body that writes the sentinel {"continue": bool} to $signal.
# It stops once its integer index (read from $idx) reaches a threshold, so
# the loop stops after exactly ``stop_at`` iterations. Zero-based index: the
# controller feeds iteration k the value k-1, so index 0 is the 1st body.
_STOP_AT_BODY = (
    'i=$(cat "$idx"); '
    'if [ "$i" -ge %(stop)d ]; then c=false; else c=true; fi; '
    'printf \'{"continue": %%s}\' "$c" > "$signal"'
)

# A body that always signals continue: only max_iterations stops it.
_ALWAYS_BODY = 'printf \'{"continue": true}\' > "$signal"'


def _body_task(command: str) -> HorusTask:
    """A minimal body template writing the sentinel and reading an index."""
    return HorusTask(
        id="body",
        name="body",
        runtime=CommandRuntime(command=command),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[FileArtifact(id="idx", path=Path("idx_in"))],
        outputs=[FileArtifact(id="signal", path=Path("signal_out"))],
    )


def _body_ids(wf: BaseWorkflow, loop_id: str = "loop") -> list[str]:
    """Ids of the injected body tasks, in iteration order."""
    prefix = f"{loop_id}#"
    return sorted(
        (t.id for t in wf.tasks if t.id.startswith(prefix)),
        key=lambda tid: int(tid.rsplit("#", 1)[1]),
    )


@pytest.mark.unit
class TestLoopControllerValidation:
    """LoopController field/marker invariants at construction."""

    def test_original_controller_has_fanout_output_no_loop_in(self) -> None:
        """The original controller (iteration 0) gains a never-written
        fanout output marker but no loop-in input (it is the trigger, with
        no incoming edge).
        """
        controller = LoopController(
            id="loop",
            name="loop",
            loop_id="loop",
            body_template={"kind": "horus_task"},
            signal_output="signal",
            max_iterations=3,
        )
        assert any(o.id == "loop.fanout" for o in controller.outputs)
        assert not any(a.id == "loop.loop_in" for a in controller.inputs)

    def test_checker_gains_loop_in_input_marker(self) -> None:
        """A controller-check instance (iteration >= 1) gains a loop-in
        input marker (the ordering-edge target from the body it follows).
        """
        checker = LoopController(
            id="loop~1",
            name="loop~1",
            loop_id="loop",
            iteration=1,
            body_template={"kind": "horus_task"},
            signal_output="signal",
            max_iterations=3,
        )
        assert any(a.id == "loop~1.loop_in" for a in checker.inputs)

    def test_markers_not_duplicated_on_reload(self) -> None:
        """Re-validating an already-marked controller does not duplicate
        markers (idempotent).
        """
        checker = LoopController(
            id="loop~1",
            name="loop~1",
            loop_id="loop",
            iteration=1,
            body_template={"kind": "horus_task"},
            signal_output="signal",
            max_iterations=3,
        )
        reloaded = LoopController.model_validate(
            checker.model_dump(mode="json")
        )
        assert sum(o.id == "loop~1.fanout" for o in reloaded.outputs) == 1
        assert sum(a.id == "loop~1.loop_in" for a in reloaded.inputs) == 1

    def test_max_iterations_must_be_positive(self) -> None:
        """max_iterations <= 0 is rejected."""
        with pytest.raises(ValueError, match="greater than 0"):
            LoopController(
                id="loop",
                name="loop",
                loop_id="loop",
                body_template={"kind": "horus_task"},
                signal_output="signal",
                max_iterations=0,
            )


@pytest.mark.unit
class TestConditionalLoopEndToEnd:
    """A loop that stops when the body writes a stop sentinel."""

    async def test_loop_stops_after_k_iterations(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The body signals stop at index 2 (its 3rd run), so exactly 3 body
        tasks run, in order, and the workflow COMPLETES.
        """
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.loop(
            id="loop",
            body=_body_task(_STOP_AT_BODY % {"stop": 2}),
            until="signal",
            max_iterations=10,
            index_input="idx",
        )

        await wf.run(trigger_id="loop")

        assert wf.status.value == "completed"
        body_ids = _body_ids(wf)
        assert body_ids == ["loop#1", "loop#2", "loop#3"]
        for task in wf.tasks:
            assert task.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)

    async def test_loop_stops_immediately_when_first_body_signals_stop(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A body that signals stop on its very first run yields exactly one
        body iteration.
        """
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.loop(
            id="loop",
            body=_body_task(_STOP_AT_BODY % {"stop": 0}),
            until="signal",
            max_iterations=10,
            index_input="idx",
        )

        await wf.run(trigger_id="loop")

        assert wf.status.value == "completed"
        assert _body_ids(wf) == ["loop#1"]


@pytest.mark.unit
class TestMaxIterationsSafetyBound:
    """max_iterations halts a body that never signals stop."""

    async def test_runaway_body_halts_at_max_iterations(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A body that always signals continue runs exactly max_iterations
        times, then the loop halts with no infinite growth and the workflow
        ends without error.
        """
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.loop(
            id="loop",
            body=_body_task(_ALWAYS_BODY),
            until="signal",
            max_iterations=4,
            index_input="idx",
        )

        await wf.run(trigger_id="loop")

        assert wf.status.value == "completed"
        assert _body_ids(wf) == [
            "loop#1",
            "loop#2",
            "loop#3",
            "loop#4",
        ]

    async def test_loop_without_index_input_still_runs(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The index_input is optional: a body that ignores its index still
        loops up to max_iterations (a synthesized anchor input is injected
        so the ordering edge has a real target).
        """
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        body = HorusTask(
            id="body",
            name="body",
            runtime=CommandRuntime(command=_ALWAYS_BODY),
            executor=ShellExecutor(),
            target=LocalTarget(),
            outputs=[FileArtifact(id="signal", path=Path("signal_out"))],
        )
        wf.loop(id="loop", body=body, until="signal", max_iterations=2)

        await wf.run(trigger_id="loop")

        assert wf.status.value == "completed"
        assert _body_ids(wf) == ["loop#1", "loop#2"]


@pytest.mark.unit
class TestLoopErrors:
    """LoopController._run raises clear errors for common misconfigs."""

    async def test_missing_orchestrator_target_raises(
        self, horus_context: HorusContext
    ) -> None:
        """A loop needs orchestrator_target to materialize per-iteration
        inputs.
        """
        del horus_context
        wf = HorusWorkflow(name="wf")
        wf.orchestrator_target = None  # type: ignore[assignment]
        wf.loop(
            id="loop",
            body=_body_task(_ALWAYS_BODY),
            until="signal",
            max_iterations=2,
            index_input="idx",
        )

        with pytest.raises(LoopConfigurationError, match="orchestrator"):
            await wf.run(trigger_id="loop")

    async def test_body_without_signal_output_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A body template missing the declared signal output is rejected."""
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        body = HorusTask(
            id="body",
            name="body",
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            outputs=[FileArtifact(id="other", path=Path("other_out"))],
        )
        wf.loop(id="loop", body=body, until="signal", max_iterations=2)

        with pytest.raises(LoopConfigurationError, match="signal"):
            await wf.run(trigger_id="loop")

    async def test_missing_index_input_on_body_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """An index_input naming an input the body does not declare is
        rejected.
        """
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        body = HorusTask(
            id="body",
            name="body",
            runtime=CommandRuntime(command=_ALWAYS_BODY),
            executor=ShellExecutor(),
            target=LocalTarget(),
            outputs=[FileArtifact(id="signal", path=Path("signal_out"))],
        )
        wf.loop(
            id="loop",
            body=body,
            until="signal",
            max_iterations=2,
            index_input="nope",
        )

        with pytest.raises(LoopConfigurationError, match="nope"):
            await wf.run(trigger_id="loop")

    async def test_bad_sentinel_json_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A body that writes a non-``{'continue': bool}`` sentinel is
        rejected when the controller-check reads it.
        """
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        body = HorusTask(
            id="body",
            name="body",
            runtime=CommandRuntime(
                command='printf \'{"nope": 1}\' > "$signal"'
            ),
            executor=ShellExecutor(),
            target=LocalTarget(),
            outputs=[FileArtifact(id="signal", path=Path("signal_out"))],
        )
        wf.loop(id="loop", body=body, until="signal", max_iterations=3)

        with pytest.raises(LoopConfigurationError, match="continue"):
            await wf.run(trigger_id="loop")


@pytest.mark.unit
class TestYamlLowering:
    """The ``loop:`` YAML block lowers to a loop_controller task."""

    def test_lower_loop_entry(self) -> None:
        """A ``loop:`` block lowers to a loop_controller dict with the
        body/until/max_iterations wiring.
        """
        entry = {
            "id": "loop",
            "loop": {
                "body": {"kind": "horus_task"},
                "until": "signal",
                "max_iterations": 5,
                "index_input": "idx",
            },
        }
        controller = lower_loop_entry(entry)

        assert controller["kind"] == "loop_controller"
        assert controller["id"] == "loop"
        assert controller["loop_id"] == "loop"
        assert controller["signal_output"] == "signal"
        assert controller["max_iterations"] == 5
        assert controller["index_input"] == "idx"
        assert controller["iteration"] == 0

    async def test_yaml_workflow_loads_and_runs(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A full YAML workflow with a ``loop:`` block loads via
        BaseWorkflow.from_yaml and runs to completion.
        """
        del horus_context
        wf_yaml = {
            "name": "loop_yaml",
            "kind": "horus_workflow",
            "tasks": [
                {
                    "id": "loop",
                    "loop": {
                        "body": {
                            "kind": "horus_task",
                            "runtime": {
                                "kind": "command",
                                "command": _STOP_AT_BODY % {"stop": 1},
                            },
                            "executor": {"kind": "shell"},
                            "target": {"kind": "local"},
                            "inputs": [
                                {
                                    "id": "idx",
                                    "kind": "file",
                                    "path": "idx_in",
                                }
                            ],
                            "outputs": [
                                {
                                    "id": "signal",
                                    "kind": "file",
                                    "path": "signal_out",
                                }
                            ],
                        },
                        "until": "signal",
                        "max_iterations": 10,
                        "index_input": "idx",
                    },
                },
            ],
        }
        wf_path = tmp_path / "wf.yaml"
        with wf_path.open("w") as fh:
            yaml.safe_dump(wf_yaml, fh)

        wf = BaseWorkflow.from_yaml(wf_path)
        assert isinstance(wf, HorusWorkflow)
        loop = next(t for t in wf.tasks if t.id == "loop")
        assert isinstance(loop, LoopController)
        wf.orchestrator_target = LocalTarget(
            working_directory=tmp_path.as_posix()
        )

        await wf.run(trigger_id="loop")

        assert wf.status.value == "completed"
        # stop at index 1 (2nd body): loop#1, loop#2.
        assert _body_ids(wf) == ["loop#1", "loop#2"]

    def test_yaml_to_yaml_round_trip(self, tmp_path: Path) -> None:
        """to_yaml -> from_yaml round-trips a loop workflow: the second
        load sees the already-lowered loop_controller natively (no
        ``loop:`` block survives the dump).
        """
        wf = HorusWorkflow(
            name="wf",
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.loop(
            id="loop",
            body=_body_task(_ALWAYS_BODY),
            until="signal",
            max_iterations=3,
            index_input="idx",
        )

        out_path = tmp_path / "dump.yaml"
        wf.to_yaml(out_path)

        dumped = yaml.safe_load(out_path.read_text())
        loop_dict = next(t for t in dumped["tasks"] if t["id"] == "loop")
        assert "loop" not in loop_dict
        assert loop_dict["kind"] == "loop_controller"

        wf2 = BaseWorkflow.from_yaml(out_path)
        assert isinstance(wf2, HorusWorkflow)
        loop2 = next(t for t in wf2.tasks if t.id == "loop")
        assert isinstance(loop2, LoopController)
        assert loop2.loop_id == "loop"
        assert loop2.signal_output == "signal"
        assert loop2.max_iterations == 3
        assert loop2.index_input == "idx"


@pytest.mark.unit
class TestPythonBuilderParity:
    """wf.loop(...) and the equivalent YAML loop: block agree structurally."""

    def test_python_builder_matches_yaml_lowering_shape(
        self,
    ) -> None:
        """Building the same loop via wf.loop(...) and via lower_loop_entry
        yields the same controller wiring.
        """
        wf = HorusWorkflow(name="wf")
        controller = wf.loop(
            id="loop",
            body=_body_task(_ALWAYS_BODY),
            until="signal",
            max_iterations=5,
            index_input="idx",
        )

        entry = {
            "id": "loop",
            "loop": {
                "body": {"kind": "horus_task"},
                "until": "signal",
                "max_iterations": 5,
                "index_input": "idx",
            },
        }
        yaml_controller = lower_loop_entry(entry)

        assert controller.loop_id == yaml_controller["loop_id"]
        assert controller.signal_output == yaml_controller["signal_output"]
        assert controller.max_iterations == yaml_controller["max_iterations"]
        assert controller.index_input == yaml_controller["index_input"]
        assert controller.iteration == yaml_controller["iteration"]

    def test_builder_appends_single_controller_no_edges(self) -> None:
        """wf.loop appends exactly one controller and, unlike collection
        map, no construction-time edge.
        """
        wf = HorusWorkflow(name="wf")
        wf.loop(
            id="loop",
            body=_body_task(_ALWAYS_BODY),
            until="signal",
            max_iterations=2,
            index_input="idx",
        )
        assert [t.id for t in wf.tasks] == ["loop"]
        assert wf.edges == []


@pytest.mark.unit
class TestBoundedLoopViaRangeMap:
    """
    #116's "bounded loop" acceptance: a counted, run-exactly-N-times loop is
    the range map from #122. This re-asserts that here so bounded and
    conditional loops are both covered in one place; see test_map.py's
    TestRangeMapEndToEnd for the full range-map fan-out/fan-in coverage.
    """

    async def test_range_map_is_the_bounded_loop(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """range=3 runs the body exactly 3 times, indexed 0/1/2."""
        del horus_context
        gather = HorusTask(
            id="gather",
            name="gather",
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            inputs=[FolderArtifact(id="results", path=Path("gather_in"))],
            outputs=[FileArtifact(id="done", path=tmp_path / "done.txt")],
        )
        template = HorusTask(
            id="template",
            name="template",
            runtime=CommandRuntime(
                command="mkdir -p $scored && cp $idx $scored/idx.json"
            ),
            executor=ShellExecutor(),
            target=LocalTarget(),
            inputs=[FileArtifact(id="idx", path=Path("idx_in"))],
            outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
        )
        wf = HorusWorkflow(
            name="wf",
            tasks=[gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.map(
            id="rmap",
            template=template,
            range=3,
            index_input="idx",
            gather=("gather", "results"),
        )

        await wf.run(trigger_id="rmap")

        assert wf.status.value == "completed"
        clone_ids = sorted(t.id for t in wf.tasks if t.id.startswith("rmap["))
        assert clone_ids == ["rmap[0]", "rmap[1]", "rmap[2]"]
