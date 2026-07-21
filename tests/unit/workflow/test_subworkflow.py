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
Unit tests for the subworkflow construct: port derivation, inlining,
boundary rewiring, the ``sub:`` YAML lowering hook and the
``wf.subworkflow(...)`` Python builder.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_builtin.workflow.subworkflow import (
    SubworkflowError,
    SubworkflowExpander,
    derive_ports,
    lower_subworkflow_entry,
)
from horus_runtime.context import HorusContext
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.core.workflow.condition import EdgeCondition
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import (
    TaskIdsAreNotUniqueError,
    UnknownEdgeEndpointError,
)


def _shell_task(
    task_id: str,
    command: str,
    *,
    inputs: list[Any] | None = None,
    outputs: list[Any] | None = None,
) -> HorusTask:
    """Build a minimal local shell task with the given artifacts."""
    return HorusTask(
        id=task_id,
        name=task_id,
        runtime=CommandRuntime(command=command),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=inputs or [],
        outputs=outputs or [],
    )


def _child_workflow(seed: Path) -> HorusWorkflow:
    """
    A two-task child workflow with one root artifact and one leaf output.

    Derived interface: in-port ``seed`` (the root artifact), out-port
    ``report_out`` (``report``'s unconsumed output).
    """
    upper = _shell_task(
        "upper",
        "tr a-z A-Z < $seed > $upped",
        inputs=[FileArtifact(id="seed", path=Path("seed_in.txt"))],
        outputs=[FileArtifact(id="upped", path=Path("upped.txt"))],
    )
    report = _shell_task(
        "report",
        "cat $upped > $report_out",
        inputs=[FileArtifact(id="upped", path=Path("upped.txt"))],
        outputs=[FileArtifact(id="report_out", path=Path("report.txt"))],
    )
    return HorusWorkflow(
        name="child",
        tasks=[upper, report],
        artifacts=[FileArtifact(id="seed", path=seed)],
        edges=[
            WorkflowEdge(
                source="artifact-seed",
                source_output="seed",
                target="upper",
                target_input="seed",
            ),
            WorkflowEdge(
                source="upper",
                source_output="upped",
                target="report",
                target_input="upped",
            ),
        ],
    )


def _task_dict(task_id: str, outputs: list[str] | None = None) -> Any:
    """A minimal serialized task dict for hand-built body documents."""
    return _shell_task(
        task_id,
        "true",
        outputs=[
            FileArtifact(id=out, path=Path(f"{out}.txt"))
            for out in outputs or []
        ],
    ).model_dump(mode="json")


def _yaml_task(
    task_id: str,
    command: str,
    *,
    inputs: list[tuple[str, str]] | None = None,
    outputs: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """A hand-written task dict, exactly as a YAML author would spell it."""
    return {
        "kind": "horus_task",
        "id": task_id,
        "name": task_id,
        "runtime": {"kind": "command", "command": command},
        "executor": {"kind": "shell"},
        "target": {"kind": "local"},
        "inputs": [
            {"kind": "file", "id": i, "path": p} for i, p in inputs or []
        ],
        "outputs": [
            {"kind": "file", "id": i, "path": p} for i, p in outputs or []
        ],
    }


def _yaml_child(seed: Path) -> dict[str, Any]:
    """The YAML spelling of :func:`_child_workflow`."""
    return {
        "kind": "horus_workflow",
        "name": "child",
        "artifacts": [{"kind": "file", "id": "seed", "path": seed.as_posix()}],
        "tasks": [
            _yaml_task(
                "upper",
                "tr a-z A-Z < $seed > $upped",
                inputs=[("seed", "seed_in.txt")],
                outputs=[("upped", "upped.txt")],
            ),
            _yaml_task(
                "report",
                "cat $upped > $report_out",
                inputs=[("upped", "upped.txt")],
                outputs=[("report_out", "report.txt")],
            ),
        ],
        "edges": [
            {
                "source": "artifact-seed",
                "source_output": "seed",
                "target": "upper",
                "target_input": "seed",
            },
            {
                "source": "upper",
                "source_output": "upped",
                "target": "report",
                "target_input": "upped",
            },
        ],
    }


def _seed_file(tmp_path: Path, text: str = "hello") -> Path:
    """Write and return a seed input file."""
    seed = tmp_path / "seed.txt"
    seed.write_text(text)
    return seed


def _parent(tmp_path: Path, **kwargs: Any) -> HorusWorkflow:
    """A parent workflow rooted at *tmp_path*."""
    return HorusWorkflow(
        name="parent",
        orchestrator_target=LocalTarget(working_directory=tmp_path.as_posix()),
        **kwargs,
    )


def _inner(wf: BaseWorkflow, task_id: str) -> Any:
    """Fetch an inlined task by its prefixed id."""
    return next(t for t in wf.tasks if t.id == task_id)


@pytest.mark.unit
class TestDerivePorts:
    """Ports are derived from the child workflow document itself."""

    def test_root_artifacts_become_in_ports(self, tmp_path: Path) -> None:
        """Every standalone root artifact is an input port."""
        body = _child_workflow(_seed_file(tmp_path)).model_dump(mode="json")
        in_ports, _out = derive_ports(body)
        assert [p.name for p in in_ports] == ["seed"]
        assert in_ports[0].artifact == "seed"
        assert in_ports[0].task is None

    def test_unconsumed_outputs_become_out_ports(self, tmp_path: Path) -> None:
        """Only outputs no inner edge consumes are exposed."""
        body = _child_workflow(_seed_file(tmp_path)).model_dump(mode="json")
        _in, out_ports = derive_ports(body)
        assert [p.name for p in out_ports] == ["report_out"]
        assert out_ports[0].task == "report"

    def test_colliding_output_ids_are_qualified(self) -> None:
        """Two leaves sharing an artifact id get ``taskid.artifactid``."""
        body = {
            "kind": "horus_workflow",
            "name": "child",
            "tasks": [
                {"id": "a", "outputs": [{"id": "out"}]},
                {"id": "b", "outputs": [{"id": "out"}]},
            ],
        }
        _in, out_ports = derive_ports(body)
        assert sorted(p.name for p in out_ports) == ["a.out", "b.out"]

    def test_port_overrides_rename_derived_ports(self, tmp_path: Path) -> None:
        """``port_overrides`` renames a derived port and nothing else."""
        body = _child_workflow(_seed_file(tmp_path)).model_dump(mode="json")
        in_ports, out_ports = derive_ports(body, {"report_out": "summary"})
        assert [p.name for p in in_ports] == ["seed"]
        assert [p.name for p in out_ports] == ["summary"]
        assert out_ports[0].artifact == "report_out"

    def test_placeholders_are_declared_for_every_port(
        self, tmp_path: Path
    ) -> None:
        """The expander declares one never-written artifact per port."""
        child = _child_workflow(_seed_file(tmp_path))
        sub = SubworkflowExpander(
            id="sub", name="sub", body=child.model_dump(mode="json")
        )
        assert [a.id for a in sub.inputs] == ["seed"]
        assert [a.id for a in sub.outputs] == ["report_out"]

    def test_placeholder_declaration_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        """Re-validating a dumped expander does not duplicate ports."""
        child = _child_workflow(_seed_file(tmp_path))
        sub = SubworkflowExpander(
            id="sub", name="sub", body=child.model_dump(mode="json")
        )
        again = SubworkflowExpander.model_validate(sub.model_dump(mode="json"))
        assert [a.id for a in again.outputs] == ["report_out"]


@pytest.mark.unit
class TestExistingWorkflowDropsInUnchanged:
    """An existing workflow can be embedded as a body with no edits."""

    async def test_unmodified_workflow_runs_as_a_subworkflow(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A workflow authored standalone is embedded verbatim: the stored
        body is byte-identical to its own dump, and running the parent
        produces the child's output.
        """
        del horus_context
        child = _child_workflow(_seed_file(tmp_path, "abc"))
        wf = _parent(tmp_path)
        sub = wf.subworkflow(id="sub", body=child)
        assert [t["id"] for t in sub.body["tasks"]] == ["upper", "report"]
        assert sub.body["edges"] == child.model_dump(mode="json")["edges"]

        await wf.run(trigger_id="sub")

        assert wf.status.value == "completed"
        report = _inner(wf, "sub/report")
        assert report.outputs[0].path.read_text().strip() == "ABC"

    async def test_child_root_artifact_survives_when_unfed(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        An in-port the parent does not feed keeps the child's own root
        artifact, re-registered under the prefixed id.
        """
        del horus_context
        child = _child_workflow(_seed_file(tmp_path, "xyz"))
        wf = _parent(tmp_path)
        wf.subworkflow(id="sub", body=child)

        await wf.run(trigger_id="sub")

        assert wf.status.value == "completed"
        assert any(a.id == "sub/seed" for a in wf.artifacts)


@pytest.mark.unit
class TestSubworkflowInliningEndToEnd:
    """The child's tasks and edges land in the parent's live DAG."""

    async def test_inner_tasks_are_prefixed_and_run(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Inner ids are ``<sub>/<inner>`` and every one of them runs."""
        del horus_context
        child = _child_workflow(_seed_file(tmp_path))
        wf = _parent(tmp_path)
        wf.subworkflow(id="sub", body=child)

        await wf.run(trigger_id="sub")

        ids = {t.id for t in wf.tasks}
        assert {"sub", "sub/upper", "sub/report"} == ids
        for task in wf.tasks:
            assert task.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)

    async def test_no_ordering_edges_are_emitted_to_inner_tasks(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Inner roots are gated by ``_gate_new_tasks_behind_creator``'s
        implicit dependency, not by an edge from the expander.
        """
        del horus_context
        child = _child_workflow(_seed_file(tmp_path))
        wf = _parent(tmp_path)
        wf.subworkflow(id="sub", body=child)

        await wf.run(trigger_id="sub")

        assert not [e for e in wf.edges if e.source == "sub"]
        assert wf.implicit_task_dependencies["sub/upper"] == {"sub"}
        # ``sub/report`` is ordered by the inner edge instead.
        assert "sub/report" not in wf.implicit_task_dependencies

    async def test_expander_never_reports_complete(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Port placeholders are never written, so the expander re-runs."""
        del horus_context
        child = _child_workflow(_seed_file(tmp_path))
        wf = _parent(tmp_path)
        sub = wf.subworkflow(id="sub", body=child)

        await wf.run(trigger_id="sub")

        assert await sub.is_complete() is False


@pytest.mark.unit
class TestBoundaryWiring:
    """Parent edges are re-emitted onto the real inner endpoints."""

    async def test_in_port_is_fed_by_a_parent_task(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A parent producer feeds the child's root artifact."""
        del horus_context
        prep = _shell_task(
            "prep",
            "printf 'from-parent' > $seed_out",
            outputs=[FileArtifact(id="seed_out", path=Path("seed_out.txt"))],
        )
        child = _child_workflow(_seed_file(tmp_path))
        wf = _parent(tmp_path, tasks=[prep])
        wf.subworkflow(id="sub", body=child)
        wf.edges.append(
            WorkflowEdge(
                source="prep",
                source_output="seed_out",
                target="sub",
                target_input="seed",
                transfer=False,
            )
        )

        await wf.run(trigger_id="prep")

        assert wf.status.value == "completed"
        report = _inner(wf, "sub/report")
        assert report.outputs[0].path.read_text().strip() == "FROM-PARENT"
        # The child's own root artifact is dropped: the parent supplies it.
        assert not any(a.id == "sub/seed" for a in wf.artifacts)

    async def test_out_port_feeds_a_parent_task(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A parent consumer receives the child's leaf output."""
        del horus_context
        final = _shell_task(
            "final",
            "cat $res > $final_out",
            inputs=[FileArtifact(id="res", path=Path("res_in.txt"))],
            outputs=[FileArtifact(id="final_out", path=Path("final.txt"))],
        )
        child = _child_workflow(_seed_file(tmp_path, "tail"))
        wf = _parent(tmp_path, tasks=[final])
        wf.subworkflow(id="sub", body=child)
        wf.edges.append(
            WorkflowEdge(
                source="sub",
                source_output="report_out",
                target="final",
                target_input="res",
                transfer=False,
            )
        )

        await wf.run(trigger_id="sub")

        assert wf.status.value == "completed"
        assert final.outputs[0].path.read_text().strip() == "TAIL"
        assert any(
            e.source == "sub/report" and e.target == "final" and e.transfer
            for e in wf.edges
        )

    def test_builder_forces_existing_boundary_edges_to_ordering(
        self, tmp_path: Path
    ) -> None:
        """``wf.subworkflow`` downgrades boundary edges it can see."""
        child = _child_workflow(_seed_file(tmp_path))
        wf = _parent(tmp_path)
        # A pre-existing edge object, wired before the builder runs.
        edge = WorkflowEdge(
            source="sub",
            source_output="report_out",
            target="sub",
            target_input="seed",
        )
        wf.edges.append(edge)
        wf.subworkflow(id="sub", body=child)
        assert edge.transfer is False


@pytest.mark.unit
class TestNestedSubworkflows:
    """A body may itself contain a subworkflow task."""

    async def test_two_levels_expand(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Ids nest as ``outer/inner/leaf``, one level per iteration."""
        del horus_context
        leaf = _child_workflow(_seed_file(tmp_path, "deep"))
        middle = _parent(tmp_path)
        middle.name = "middle"
        middle.subworkflow(id="inner", body=leaf)

        wf = _parent(tmp_path)
        wf.subworkflow(id="outer", body=middle)

        await wf.run(trigger_id="outer")

        assert wf.status.value == "completed"
        ids = {t.id for t in wf.tasks}
        assert "outer/inner" in ids
        assert "outer/inner/report" in ids
        report = _inner(wf, "outer/inner/report")
        assert report.outputs[0].path.read_text().strip() == "DEEP"


@pytest.mark.unit
class TestSubworkflowAsMapTemplate:
    """A subworkflow validates and expands as a ``map:`` template."""

    async def test_mapped_subworkflow_runs_per_clone(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Each map clone inlines its own copy of the body, with per-clone
        paths. Fan-in of a mapped subworkflow's *results* is a known gap:
        ``MapExpander._pin_path`` pins the port placeholder rather than
        the inner producer's real output.
        """
        del horus_context
        emit = _shell_task(
            "emit",
            "mkdir -p $out && cp $idx $out/idx.json",
            inputs=[FileArtifact(id="idx", path=Path("idx_in.json"))],
            outputs=[FolderArtifact(id="out", path=Path("emit_out"))],
        )
        body = HorusWorkflow(
            name="child",
            tasks=[emit],
            artifacts=[FileArtifact(id="idx", path=tmp_path / "idx.json")],
            edges=[
                WorkflowEdge(
                    source="artifact-idx",
                    source_output="idx",
                    target="emit",
                    target_input="idx",
                )
            ],
        )
        template = SubworkflowExpander(id="tpl", name="tpl", body=body)
        gather = _shell_task(
            "gather",
            "true",
            inputs=[FolderArtifact(id="results", path=Path("gather_in"))],
            outputs=[FileArtifact(id="done", path=tmp_path / "done.txt")],
        )
        wf = _parent(tmp_path, tasks=[gather])
        wf.map(
            id="fan",
            template=template,
            range=2,
            index_input="idx",
            gather=("gather", "results"),
        )

        await wf.run(trigger_id="fan")

        assert wf.status.value == "completed"
        for i in range(2):
            inner = _inner(wf, f"fan[{i}]/emit")
            assert inner.status is TaskStatus.COMPLETED
            assert (inner.outputs[0].path / "idx.json").read_text() == str(i)


@pytest.mark.unit
class TestConditionInsideSubworkflow:
    """A conditional edge inside a body still branches once inlined."""

    async def test_inner_condition_gates_an_inner_task(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The false branch is skipped, the true branch completes."""
        del horus_context
        decide = _shell_task(
            "decide",
            "printf '{\"go\": true}' > $verdict",
            outputs=[FileArtifact(id="verdict", path=Path("verdict.json"))],
        )
        taken = _shell_task(
            "taken",
            "printf yes > $taken_out",
            outputs=[FileArtifact(id="taken_out", path=Path("taken.txt"))],
        )
        skipped = _shell_task(
            "skipped",
            "printf no > $skipped_out",
            outputs=[FileArtifact(id="skipped_out", path=Path("skip.txt"))],
        )
        body = HorusWorkflow(
            name="child",
            tasks=[decide, taken, skipped],
            edges=[
                WorkflowEdge(
                    source="decide",
                    target="taken",
                    condition=EdgeCondition(
                        source_task="decide",
                        source_output="verdict",
                        key="go",
                        op="eq",
                        value=True,
                    ),
                ),
                WorkflowEdge(
                    source="decide",
                    target="skipped",
                    condition=EdgeCondition(
                        source_task="decide",
                        source_output="verdict",
                        key="go",
                        op="eq",
                        value=False,
                    ),
                ),
            ],
        )
        wf = _parent(tmp_path)
        wf.subworkflow(id="sub", body=body)

        await wf.run(trigger_id="sub")

        assert _inner(wf, "sub/taken").status is TaskStatus.COMPLETED
        assert _inner(wf, "sub/skipped").status is TaskStatus.SKIPPED

    def test_inner_conditions_are_carried_through_verbatim(
        self, tmp_path: Path
    ) -> None:
        """The lowered inner edge keeps the body's condition."""
        del tmp_path
        body = {
            "kind": "horus_workflow",
            "name": "child",
            "tasks": [
                _task_dict("a", ["flag"]),
                _task_dict("b", ["out"]),
            ],
            "edges": [
                {
                    "source": "a",
                    "target": "b",
                    "condition": {
                        "kind": "declarative",
                        "source_task": "a",
                        "source_output": "flag",
                        "op": "truthy",
                    },
                }
            ],
        }
        sub = SubworkflowExpander(id="sub", name="sub", body=body)
        # ``flag`` is consumed by the inner edge only as a condition
        # source, so it stays an out-port alongside ``out``.
        assert {a.id for a in sub.outputs} == {"flag", "out"}


@pytest.mark.unit
class TestYamlLowering:
    """The ``sub:`` sugar lowers to a native ``kind: subworkflow`` task."""

    def test_sub_block_lowers_to_subworkflow_task(self) -> None:
        """``lower_subworkflow_entry`` produces a native task dict."""
        entry = {
            "id": "sub",
            "sub": {"kind": "horus_workflow", "name": "child", "tasks": []},
            "port_overrides": {"a": "b"},
        }
        lowered = lower_subworkflow_entry(entry)
        assert lowered["kind"] == "subworkflow"
        assert lowered["id"] == "sub"
        assert lowered["body"]["name"] == "child"
        assert lowered["port_overrides"] == {"a": "b"}

    async def test_yaml_workflow_with_sub_block_runs(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A ``sub:`` block loads, forces ordering edges, and runs."""
        del horus_context
        child = _yaml_child(_seed_file(tmp_path, "yamlish"))
        doc: dict[str, Any] = {
            "kind": "horus_workflow",
            "name": "parent",
            "orchestrator_target": {
                "kind": "local",
                "working_directory": tmp_path.as_posix(),
            },
            "tasks": [
                _yaml_task(
                    "final",
                    "cat $res > $final_out",
                    inputs=[("res", "res_in.txt")],
                    outputs=[("final_out", "final.txt")],
                ),
                {"id": "sub", "sub": child},
            ],
            "edges": [
                {
                    "source": "sub",
                    "source_output": "report_out",
                    "target": "final",
                    "target_input": "res",
                }
            ],
        }
        path = tmp_path / "wf.yaml"
        with path.open("w") as fh:
            yaml.safe_dump(doc, fh)

        wf = BaseWorkflow.from_yaml(path)
        boundary = next(e for e in wf.edges if e.source == "sub")
        assert boundary.transfer is False

        await wf.run(trigger_id="sub")

        assert wf.status.value == "completed"
        final = _inner(wf, "final")
        assert final.outputs[0].path.read_text().strip() == "YAMLISH"

    def test_yaml_round_trip_dumps_native_kind(self, tmp_path: Path) -> None:
        """``to_yaml`` emits ``kind: subworkflow``, never ``sub:``."""
        child = _child_workflow(_seed_file(tmp_path))
        wf = _parent(tmp_path)
        wf.subworkflow(id="sub", body=child)

        out = tmp_path / "dump.yaml"
        wf.to_yaml(out)
        dumped = yaml.safe_load(out.read_text())
        entry = next(t for t in dumped["tasks"] if t["id"] == "sub")
        assert "sub" not in entry
        assert entry["kind"] == "subworkflow"

        wf2 = BaseWorkflow.from_yaml(out)
        sub2 = next(t for t in wf2.tasks if t.id == "sub")
        assert isinstance(sub2, SubworkflowExpander)
        assert [a.id for a in sub2.outputs] == ["report_out"]


@pytest.mark.unit
class TestPythonBuilderParity:
    """wf.subworkflow(...) and the YAML sugar agree structurally."""

    def test_builder_matches_yaml_lowering_shape(self, tmp_path: Path) -> None:
        """Both authoring paths produce the same expander fields."""
        child = _child_workflow(_seed_file(tmp_path))

        wf = _parent(tmp_path)
        built = wf.subworkflow(id="sub", body=child)

        lowered = lower_subworkflow_entry({"id": "sub", "sub": child})
        from_yaml_style = SubworkflowExpander.model_validate(lowered)

        assert built.kind == from_yaml_style.kind
        assert built.body == from_yaml_style.body
        assert [a.id for a in built.inputs] == [
            a.id for a in from_yaml_style.inputs
        ]
        assert [a.id for a in built.outputs] == [
            a.id for a in from_yaml_style.outputs
        ]


@pytest.mark.unit
class TestSubworkflowErrors:
    """Malformed subworkflows are rejected, mostly at load time."""

    def test_duplicate_inner_task_id_is_rejected(self) -> None:
        """The body is validated by constructing a real workflow."""
        body = {
            "kind": "horus_workflow",
            "name": "child",
            "tasks": [_task_dict("a"), _task_dict("a")],
        }
        with pytest.raises(TaskIdsAreNotUniqueError):
            SubworkflowExpander(id="sub", name="sub", body=body)

    def test_slash_in_inner_id_is_rejected(self) -> None:
        """``/`` is reserved for the inlined id prefix."""
        body = {
            "kind": "horus_workflow",
            "name": "child",
            "tasks": [{"id": "a/b"}],
        }
        with pytest.raises(SubworkflowError, match="'/'"):
            SubworkflowExpander(id="sub", name="sub", body=body)

    def test_unresolved_inner_edge_is_rejected(self) -> None:
        """Existing edge validation is reused, not reimplemented."""
        body = {
            "kind": "horus_workflow",
            "name": "child",
            "tasks": [_task_dict("a")],
            "edges": [{"source": "a", "target": "nope"}],
        }
        with pytest.raises(UnknownEdgeEndpointError):
            SubworkflowExpander(id="sub", name="sub", body=body)

    async def test_depth_guard_stops_runaway_nesting(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """``max_depth`` fails the run rather than expanding forever."""
        del horus_context
        child = _child_workflow(_seed_file(tmp_path))
        wf = _parent(tmp_path)
        wf.subworkflow(id="sub", body=child, max_depth=0)

        with pytest.raises(SubworkflowError, match="max_depth"):
            await wf.run(trigger_id="sub")

    async def test_transferring_boundary_edge_is_rejected(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A ``transfer=True`` boundary edge would duplicate the data."""
        del horus_context
        child = _child_workflow(_seed_file(tmp_path))
        final = _shell_task(
            "final",
            "true",
            inputs=[FileArtifact(id="res", path=Path("res_in.txt"))],
            outputs=[FileArtifact(id="final_out", path=Path("final.txt"))],
        )
        wf = _parent(tmp_path, tasks=[final])
        wf.subworkflow(id="sub", body=child)
        wf.edges.append(
            WorkflowEdge(
                source="sub",
                source_output="report_out",
                target="final",
                target_input="res",
            )
        )

        with pytest.raises(SubworkflowError, match="transfer=False"):
            await wf.run(trigger_id="sub")


@pytest.mark.unit
class TestSubworkflowReset:
    """Reset clears the expander's own (never-written) placeholders."""

    async def test_reset_clears_run_count(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """``_reset`` zeroes ``runs`` and leaves the body untouched."""
        del horus_context
        child = _child_workflow(_seed_file(tmp_path))
        wf = _parent(tmp_path)
        sub = wf.subworkflow(id="sub", body=child)

        await wf.run(trigger_id="sub")
        assert sub.runs == 1

        await sub._reset()

        assert sub.runs == 0
        assert [a.id for a in sub.outputs] == ["report_out"]
