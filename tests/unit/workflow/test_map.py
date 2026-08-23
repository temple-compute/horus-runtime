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
Unit tests for the ``horus_map`` task: MapTask, its port validation, and its
fan-out/fan-in behaviour as an ordinary DAG node.
"""

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.artifact.json import JSONArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python_string import PythonCodeStringRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_builtin.workflow.map import MapTask
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.core.workflow.edge import WorkflowEdge


def _template_task(
    *,
    item_id: str = "item",
    output_id: str = "result",
    command: str = "cp $item $result",
    extra_inputs: list[FileArtifact] | None = None,
) -> HorusTask:
    """A minimal per-clone template task: copies its item to its output."""
    return HorusTask(
        id="template",
        name="template",
        runtime=CommandRuntime(command=command),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[
            FileArtifact(id=item_id, path=Path("item_in")),
            *(extra_inputs or []),
        ],
        outputs=[FileArtifact(id=output_id, path=Path("result.txt"))],
    )


def _split_task(tmp_path: Path, names: list[str]) -> HorusTask:
    """A task whose FolderArtifact output already has one file per name,
    each holding its own name as content.
    """
    batches = tmp_path / "batches"
    batches.mkdir(parents=True, exist_ok=True)
    for name in names:
        (batches / name).write_text(name)
    return HorusTask(
        id="split",
        name="split",
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        outputs=[FolderArtifact(id="batches", path=batches)],
    )


def _json_split_task(tmp_path: Path, items: list[str]) -> HorusTask:
    """A task whose JSON-list output already contains *items*."""
    artifact = JSONArtifact(id="batches", path=tmp_path / "batches.json")
    artifact.write(items)
    return HorusTask(
        id="split",
        name="split",
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        outputs=[artifact],
    )


def _map_task(
    *,
    over_artifact: BaseArtifact,
    task: HorusTask,
    item_input: str = "item",
    output_path: str = "scored_out",
    extra_inputs: list[FileArtifact] | None = None,
    max_concurrency: int | None = None,
) -> MapTask:
    """A ``horus_map`` task fanning *task* out over *over_artifact*."""
    return MapTask(
        id="score",
        name="score",
        over=over_artifact.id,
        item_input=item_input,
        inputs=[over_artifact, *(extra_inputs or [])],
        outputs=[FolderArtifact(id="scored", path=Path(output_path))],
        task=task,
        max_concurrency=max_concurrency,
    )


def _wire(
    tmp_path: Path,
    *,
    split: HorusTask,
    map_task: MapTask,
    extra_tasks: list[HorusTask] | None = None,
    extra_edges: list[WorkflowEdge] | None = None,
    artifacts: list[BaseArtifact] | None = None,
) -> HorusWorkflow:
    """Wire *split* -> *map_task* with an ordinary, real edge."""
    return HorusWorkflow(
        name="wf",
        tasks=[split, map_task, *(extra_tasks or [])],
        artifacts=artifacts or [],
        edges=[
            WorkflowEdge(
                source=split.id,
                source_output=split.outputs[0].id,
                target=map_task.id,
                target_input=map_task.over,
            ),
            *(extra_edges or []),
        ],
        orchestrator_target=LocalTarget(working_directory=tmp_path.as_posix()),
    )


@pytest.mark.unit
class TestFolderFanOut:
    """Fan-out over a FolderArtifact source: one slot per child, named
    after the child.
    """

    async def test_three_files_fan_out_and_land_in_named_slots(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Each child file becomes its own slot, named after itself."""
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            task=_template_task(),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == [
            "a.txt",
            "b.txt",
            "c.txt",
        ]
        for name in ("a.txt", "b.txt", "c.txt"):
            assert (scored / name / "result.txt").read_text() == name

    async def test_downstream_task_consumes_the_folder_output(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The map's declared folder output is an ordinary output any
        downstream task can consume via a normal edge.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            task=_template_task(),
        )
        analysis = HorusTask(
            id="analysis",
            name="analysis",
            runtime=CommandRuntime(command="ls $scored > $report"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            inputs=[FolderArtifact(id="scored", path=Path("scored_in"))],
            outputs=[FileArtifact(id="report", path=tmp_path / "report.txt")],
        )
        wf = _wire(
            tmp_path,
            split=split,
            map_task=map_task,
            extra_tasks=[analysis],
            extra_edges=[
                WorkflowEdge(
                    source="score",
                    source_output="scored",
                    target="analysis",
                    target_input="scored",
                )
            ],
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        report = (tmp_path / "report.txt").read_text()
        assert "a.txt" in report
        assert "b.txt" in report

    async def test_clones_are_registered_as_ordinary_dag_tasks(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Clones land in ``wf.tasks`` (via ``expand()``), ordered after the
        map by an artifact-less edge, so a live dashboard, a workflow dump,
        or a resumed run all see them like any other task.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            task=_template_task(),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        clone_ids = {"score[a.txt]", "score[b.txt]"}
        clones = [t for t in wf.tasks if t.id in clone_ids]
        assert {c.id for c in clones} == clone_ids
        assert all(c.status == TaskStatus.COMPLETED for c in clones)
        assert {
            (e.source, e.target) for e in wf.edges if e.target in clone_ids
        } == {("score", "score[a.txt]"), ("score", "score[b.txt]")}


@pytest.mark.unit
class TestListFanOut:
    """Fan-out over a non-folder source whose read() returns a JSON list:
    one slot per element, zero-padded by index.
    """

    async def test_two_element_list_fans_out(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A 2-element list yields slots "0" and "1"."""
        del horus_context
        split = _json_split_task(tmp_path, ["x", "y"])
        map_task = _map_task(
            over_artifact=JSONArtifact(
                id="batches", path=Path("batches_in.json")
            ),
            task=_template_task(),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["0", "1"]
        assert (scored / "0" / "result.txt").read_text() == "x"
        assert (scored / "1" / "result.txt").read_text() == "y"

    async def test_slot_index_is_zero_padded_to_the_widest_index(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """11 elements (max index 10, two digits) pad every slot to width 2."""
        del horus_context
        items = [f"v{i}" for i in range(11)]
        split = _json_split_task(tmp_path, items)
        map_task = _map_task(
            over_artifact=JSONArtifact(
                id="batches", path=Path("batches_in.json")
            ),
            task=_template_task(),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        slots = sorted(p.name for p in scored.iterdir())
        assert slots[0] == "00"
        assert slots[-1] == "10"
        assert len(slots) == 11
        assert (scored / "00" / "result.txt").read_text() == "v0"
        assert (scored / "10" / "result.txt").read_text() == "v10"


@pytest.mark.unit
class TestSharedInput:
    """An input declared on the map itself, other than ``over``, is shared
    verbatim by every clone.
    """

    async def test_static_root_artifact_reaches_every_clone_unchanged(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Every clone reads the exact same shared file."""
        del horus_context
        (tmp_path / "receptor.txt").write_text("RECEPTOR")
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        template = _template_task(
            command="cat $item $receptor > $result",
            extra_inputs=[FileArtifact(id="receptor", path=Path("rec_in"))],
        )
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            task=template,
            extra_inputs=[
                FileArtifact(id="receptor", path=Path("receptor_in"))
            ],
        )
        wf = _wire(
            tmp_path,
            split=split,
            map_task=map_task,
            artifacts=[
                FileArtifact(id="receptor", path=tmp_path / "receptor.txt")
            ],
            extra_edges=[
                WorkflowEdge(
                    source="artifact-receptor",
                    source_output="receptor",
                    target="score",
                    target_input="receptor",
                )
            ],
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        for name in ("a.txt", "b.txt"):
            content = (scored / name / "result.txt").read_text()
            assert content == f"{name}RECEPTOR"

    async def test_undeclared_shared_input_is_wired_via_the_adopted_port(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A wrapped-task input the author never re-declares on the map is
        still reachable by an edge, via the port the map adopted for it.
        """
        del horus_context
        (tmp_path / "receptor.txt").write_text("RECEPTOR")
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        template = _template_task(
            command="cat $item $receptor > $result",
            extra_inputs=[FileArtifact(id="receptor", path=Path("rec_in"))],
        )
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            task=template,
        )
        wf = _wire(
            tmp_path,
            split=split,
            map_task=map_task,
            artifacts=[
                FileArtifact(id="receptor", path=tmp_path / "receptor.txt")
            ],
            extra_edges=[
                WorkflowEdge(
                    source="artifact-receptor",
                    source_output="receptor",
                    target="score",
                    target_input="receptor",
                )
            ],
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        for name in ("a.txt", "b.txt"):
            content = (scored / name / "result.txt").read_text()
            assert content == f"{name}RECEPTOR"


@pytest.mark.unit
class TestEmptyCollection:
    """An empty collection is a valid, trivial map."""

    async def test_empty_folder_completes_with_an_empty_output(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Zero children -> zero clones, an empty (but existing) output
        folder, and the map still reports completed.
        """
        del horus_context
        split = _split_task(tmp_path, [])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            task=_template_task(),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        assert scored.is_dir()
        assert list(scored.iterdir()) == []


@pytest.mark.unit
class TestPartialResume:
    """Once the map itself decides to re-run, each clone's own
    ``skip_if_complete`` still governs it individually.
    """

    async def test_only_the_invalidated_slot_reruns(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A FolderArtifact output's completeness is existence-only, so
        deleting one slot alone does not invalidate the map's own record;
        this also removes the map's manifest to force a fresh check (e.g.
        what a user asking for a rerun would do). Slots whose own output
        and manifest are untouched are then skipped -- proven here by
        poisoning their content and confirming it survives -- while the
        genuinely incomplete slot is rebuilt from real input.
        """
        del horus_context

        def _build() -> HorusWorkflow:
            split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
            map_task = _map_task(
                over_artifact=FolderArtifact(
                    id="batches", path=Path("batches_in")
                ),
                task=_template_task(),
            )
            return _wire(tmp_path, split=split, map_task=map_task)

        await _build().run(trigger_id="split")

        scored = tmp_path / "scored_out"
        for name in ("a.txt", "c.txt"):
            (scored / name / "result.txt").write_text("STALE")
        shutil.rmtree(scored / "b.txt")
        (tmp_path / ".horus" / "score[b.txt].json").unlink()
        (tmp_path / ".horus" / "score.json").unlink()

        wf = _build()
        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert (scored / "a.txt" / "result.txt").read_text() == "STALE"
        assert (scored / "c.txt" / "result.txt").read_text() == "STALE"
        assert (scored / "b.txt" / "result.txt").read_text() == "b.txt"


@pytest.mark.unit
class TestSkipPropagation:
    """Forcing the map's own ``skip_if_complete`` off propagates to every
    clone (mirrors the CLI's ``--no-skip-all``/``--no-skip``).
    """

    async def test_forced_rerun_reaches_every_already_complete_clone(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """With skip_if_complete forced False, no clone is skipped even
        though every slot is already complete.
        """
        del horus_context

        def _build() -> HorusWorkflow:
            split = _split_task(tmp_path, ["a.txt", "b.txt"])
            map_task = _map_task(
                over_artifact=FolderArtifact(
                    id="batches", path=Path("batches_in")
                ),
                task=_template_task(),
            )
            return _wire(tmp_path, split=split, map_task=map_task)

        await _build().run(trigger_id="split")

        scored = tmp_path / "scored_out"
        for name in ("a.txt", "b.txt"):
            (scored / name / "result.txt").write_text("STALE")

        wf = _build()
        score = next(t for t in wf.tasks if t.id == "score")
        score.skip_if_complete = False
        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        for name in ("a.txt", "b.txt"):
            assert (scored / name / "result.txt").read_text() == name


@pytest.mark.unit
class TestCloneFailure:
    """A clone failure fails the map as a whole."""

    async def test_a_failing_clone_fails_the_map(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The map's own status ends up FAILED when any clone fails."""
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            task=_template_task(command="exit 1"),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        with pytest.raises(Exception):  # noqa: B017
            await wf.run(trigger_id="split")

        assert wf.status.value == "failed"


@pytest.mark.unit
class TestConcurrency:
    """``max_concurrency`` bounds how many clones run at once."""

    async def test_max_concurrency_one_serializes_clones(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """With max_concurrency=1, no clone's window overlaps another's."""
        del horus_context
        log = tmp_path / "log.txt"
        split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
        template = HorusTask(
            id="template",
            name="template",
            runtime=CommandRuntime(
                command=(
                    f"n=$(basename $item); echo start-$n >> {log} && "
                    f"sleep 0.05 && echo end-$n >> {log} && "
                    "cp $item $result"
                )
            ),
            executor=ShellExecutor(),
            target=LocalTarget(),
            inputs=[FileArtifact(id="item", path=Path("item_in"))],
            outputs=[FileArtifact(id="result", path=Path("result.txt"))],
        )
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            task=template,
            max_concurrency=1,
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        lines = log.read_text().splitlines()
        assert len(lines) == 6
        for i in range(0, len(lines), 2):
            assert lines[i].startswith("start-")
            name = lines[i].removeprefix("start-")
            assert lines[i + 1] == f"end-{name}"


@pytest.mark.unit
class TestMapPorts:
    """Port wiring is validated at load time, not mid-run."""

    def test_over_names_unknown_input(self) -> None:
        """``over`` must name one of this task's own inputs."""
        with pytest.raises(ValidationError, match="unknown input"):
            MapTask(
                id="score",
                name="score",
                over="missing",
                item_input="item",
                inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
                outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
                task=_template_task(),
            )

    def test_item_input_names_unknown_input_on_wrapped_task(self) -> None:
        """``item_input`` must name one of the wrapped task's inputs."""
        with pytest.raises(ValidationError, match="wrapped task"):
            MapTask(
                id="score",
                name="score",
                over="batches",
                item_input="missing",
                inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
                outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
                task=_template_task(),
            )

    def test_unshared_wrapped_input_is_adopted_as_a_map_port(self) -> None:
        """A wrapped-task input other than item_input, not declared on the
        map, is adopted as one of the map's own inputs automatically.
        """
        template = _template_task(
            extra_inputs=[FileArtifact(id="receptor", path=Path("rec_in"))]
        )
        map_task = MapTask(
            id="score",
            name="score",
            over="batches",
            item_input="item",
            inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
            outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
            task=template,
        )
        adopted = next(a for a in map_task.inputs if a.id == "receptor")
        assert adopted.declared_path == Path("rec_in")

    def test_author_declared_input_wins_over_adoption(self) -> None:
        """A map input sharing an id with a wrapped-task input keeps its
        own declared path rather than being overwritten.
        """
        template = _template_task(
            extra_inputs=[FileArtifact(id="receptor", path=Path("rec_in"))]
        )
        map_task = MapTask(
            id="score",
            name="score",
            over="batches",
            item_input="item",
            inputs=[
                FolderArtifact(id="batches", path=Path("batches_in")),
                FileArtifact(id="receptor", path=Path("receptor_in")),
            ],
            outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
            task=template,
        )
        assert (
            next(a for a in map_task.inputs if a.id == "receptor").path.name
            == "receptor_in"
        )

    def test_wrong_output_count_is_rejected(self) -> None:
        """Exactly one FolderArtifact output is required."""
        with pytest.raises(ValidationError, match="FolderArtifact"):
            MapTask(
                id="score",
                name="score",
                over="batches",
                item_input="item",
                inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
                outputs=[],
                task=_template_task(),
            )

    def test_non_folder_output_is_rejected(self) -> None:
        """A single but non-folder output is also rejected."""
        with pytest.raises(ValidationError, match="FolderArtifact"):
            MapTask(
                id="score",
                name="score",
                over="batches",
                item_input="item",
                inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
                outputs=[FileArtifact(id="scored", path=Path("scored.txt"))],
                task=_template_task(),
            )


@pytest.mark.unit
class TestRoundTrip:
    """to_yaml/from_yaml preserves relative declared paths on the wrapped
    task and reruns cleanly from the reloaded document.
    """

    async def test_yaml_round_trip_preserves_relative_paths_and_reruns(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Dumped *after* a run (so the workflow's own paths are already
        anchored, not eagerly CWD-resolved): the wrapped task's own
        declared paths stay relative in the document (this task's own
        ``_dump_task`` serializer), and the reloaded workflow reruns
        cleanly (skipped via its manifest, since nothing changed).
        """
        del horus_context
        (tmp_path / "receptor.txt").write_text("RECEPTOR")
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        template = _template_task(
            extra_inputs=[FileArtifact(id="receptor", path=Path("rec_in"))]
        )
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            task=template,
        )
        wf = _wire(
            tmp_path,
            split=split,
            map_task=map_task,
            artifacts=[
                FileArtifact(id="receptor", path=tmp_path / "receptor.txt")
            ],
            extra_edges=[
                WorkflowEdge(
                    source="artifact-receptor",
                    source_output="receptor",
                    target="score",
                    target_input="receptor",
                )
            ],
        )
        await wf.run(trigger_id="split")
        assert wf.status.value == "completed"

        out_path = tmp_path / "dump.yaml"
        wf.to_yaml(out_path)

        dumped = yaml.safe_load(out_path.read_text())
        score_dict = next(t for t in dumped["tasks"] if t["id"] == "score")
        assert score_dict["kind"] == "horus_map"
        inner = score_dict["task"]
        assert inner["inputs"][0]["path"] == "item_in"
        assert inner["outputs"][0]["path"] == "result.txt"

        wf2 = BaseWorkflow.from_yaml(out_path)
        assert isinstance(wf2, HorusWorkflow)
        score2 = next(t for t in wf2.tasks if t.id == "score")
        assert isinstance(score2, MapTask)
        assert score2.over == "batches"
        assert score2.item_input == "item"
        # The adopted "receptor" port survives the round trip without
        # being adopted a second time.
        assert [a.id for a in score2.inputs].count("receptor") == 1

        await wf2.run(trigger_id="split")

        assert wf2.status.value == "completed"
        scored = tmp_path / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["a.txt", "b.txt"]


VINA_WORKFLOW = """
kind: horus_workflow
name: Vina Shaped
artifacts:
  - id: receptor
    kind: file
    path: rec.pdbqt
tasks:
  - kind: horus_task
    id: prep
    name: Prepare ligands
    outputs:
      - id: ligands
        kind: folder
        path: prepared
    executor: {kind: shell}
    runtime:
      kind: command
      command: >-
        mkdir -p $ligands &&
        echo A > $ligands/ligand_A.pdbqt &&
        echo B > $ligands/ligand_B.pdbqt
    target: {kind: local}
  - kind: horus_map
    id: dock
    name: Dock every ligand
    over: ligands
    item_input: ligand
    inputs:
      - {kind: folder, id: ligands, path: ligands_in}
    outputs:
      - {kind: folder, id: complexes, path: complexes}
    task:
      kind: horus_task
      inputs:
        - {kind: file, id: ligand, path: lig.pdbqt}
        - {kind: file, id: receptor, path: rec.pdbqt}
      outputs:
        - {kind: file, id: complex, path: complex.pdb}
      runtime:
        kind: command
        command: "cat $ligand $receptor > $complex"
      executor: {kind: shell}
      target: {kind: local}
  - kind: horus_task
    id: analysis
    name: Analysis
    inputs:
      - id: complexes
        kind: folder
        path: complexes_in
    outputs:
      - id: report
        kind: file
        path: report.txt
    executor: {kind: shell}
    runtime:
      kind: command
      command: "ls $complexes > $report"
    target: {kind: local}

edges:
  - source: prep
    source_output: ligands
    target: dock
    target_input: ligands
  - source: artifact-receptor
    source_output: receptor
    target: dock
    target_input: receptor
  - source: dock
    source_output: complexes
    target: analysis
    target_input: complexes
"""


@pytest.mark.unit
class TestVinaShapedEndToEnd:
    """The target shape from the design doc: a folder fan-out with a
    constant, shared root-artifact input, feeding a downstream consumer.
    """

    async def test_vina_shaped_workflow_runs_end_to_end(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """One slot per ligand; every clone sees the same receptor file."""
        del horus_context
        (tmp_path / "rec.pdbqt").write_text("REC")
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text(VINA_WORKFLOW)

        wf = BaseWorkflow.from_yaml(wf_path)
        assert isinstance(wf, HorusWorkflow)
        dock = next(t for t in wf.tasks if t.id == "dock")
        assert isinstance(dock, MapTask)

        await wf.run(trigger_id="prep")

        assert wf.status.value == "completed"
        complexes = tmp_path / "complexes"
        slots = sorted(p.name for p in complexes.iterdir())
        assert slots == ["ligand_A.pdbqt", "ligand_B.pdbqt"]

        receptor_lines = set()
        for slot in slots:
            lines = (complexes / slot / "complex.pdb").read_text().splitlines()
            assert len(lines) == 2
            receptor_lines.add(lines[1])
        # Every clone saw the same receptor file.
        assert receptor_lines == {"REC"}

        report = (tmp_path / "report.txt").read_text()
        assert "ligand_A.pdbqt" in report
        assert "ligand_B.pdbqt" in report


FAN_OUT_TRANSFORM_WORKFLOW = """
kind: horus_workflow
name: Transform Fanout
artifacts:
  - id: ligand
    kind: string
    path: ligand.txt
    value: LIGAND
tasks:
  - kind: horus_map
    id: expand
    name: Expand ligands
    over: ligand
    item_input: ligand
    fan_out: {kind: json, id: batches, path: batches.json}
    inputs:
      - {kind: string, id: ligand, path: ligand_in.txt}
    outputs:
      - {kind: folder, id: scored, path: scored_out}
    runtime:
      kind: command
      command: printf '[\\"a\\",\\"b\\"]' > $batches
    executor: {kind: shell}
    target: {kind: local}
    task:
      kind: horus_task
      inputs:
        - {kind: file, id: ligand, path: lig.pdbqt}
      outputs:
        - {kind: file, id: result, path: result.txt}
      runtime:
        kind: command
        command: "cat $ligand > $result"
      executor: {kind: shell}
      target: {kind: local}

edges:
  - source: artifact-ligand
    source_output: ligand
    target: expand
    target_input: ligand
"""


@pytest.mark.unit
class TestFanOutTransform:
    """A non-iterable 'over' input is legal when a ``fan_out`` transform
    body produces the iterable collection the clones fan out over.
    """

    async def test_body_produces_the_collection_that_drives_the_fan_out(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The body's JSON list becomes one clone per element; the
        non-iterable string input never needs iterating itself.
        """
        del horus_context
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text(FAN_OUT_TRANSFORM_WORKFLOW)

        wf = BaseWorkflow.from_yaml(wf_path)
        assert isinstance(wf, HorusWorkflow)
        expand = next(t for t in wf.tasks if t.id == "expand")
        assert isinstance(expand, MapTask)
        assert expand.fan_out is not None
        assert expand.fan_out.id == "batches"

        await wf.run(trigger_id="expand")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        slots = sorted(p.name for p in scored.iterdir())
        assert slots == ["0", "1"]
        assert (scored / "0" / "result.txt").read_text() == "a"
        assert (scored / "1" / "result.txt").read_text() == "b"


def _gather_map_task(gather_command: str) -> MapTask:
    """A ``horus_map`` folding its clones into a single file output."""
    return MapTask(
        id="score",
        name="score",
        over="batches",
        item_input="item",
        inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
        outputs=[FileArtifact(id="merged", path=Path("merged.txt"))],
        task=_template_task(),
        gather=CommandRuntime(command=gather_command),
    )


@pytest.mark.unit
class TestGather:
    """With ``gather``, clones re-root under an internal ``slots`` folder
    and a second transform folds it into the single declared output.
    """

    async def test_gather_folds_slots_into_the_single_output(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Every clone's output lands under the slots folder, which the
        gather command concatenates into the declared file output.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _gather_map_task("cat $slots/*/result.txt > $merged")
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert (tmp_path / "merged.txt").read_text() == "a.txtb.txt"

    async def test_slots_folder_is_reported_as_a_side_artifact(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The internal re-root survives as an inspectable side artifact."""
        del horus_context
        split = _split_task(tmp_path, ["a.txt"])
        map_task = _gather_map_task("cat $slots/*/result.txt > $merged")
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        score = next(t for t in wf.tasks if t.id == "score")
        side_ids = [a.id for a in score.side_artifacts]
        # Other side artifacts (per-task logs) may ride along; the internal
        # slots folder lands exactly once among them.
        assert side_ids.count("slots") == 1
        slots_artifact = next(
            a for a in score.side_artifacts if a.id == "slots"
        )
        assert isinstance(slots_artifact, FolderArtifact)
        assert sorted(p.name for p in Path(slots_artifact.path).iterdir()) == [
            "a.txt"
        ]

    async def test_failing_gather_fails_the_map(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A gather error propagates as an ordinary executor failure."""
        del horus_context
        split = _split_task(tmp_path, ["a.txt"])
        map_task = _gather_map_task("exit 3")
        wf = _wire(tmp_path, split=split, map_task=map_task)

        with pytest.raises(Exception):  # noqa: B017
            await wf.run(trigger_id="split")

        assert wf.status.value == "failed"


@pytest.mark.unit
class TestTransformPorts:
    """Wiring rules for the optional fan-out/gather transforms."""

    @staticmethod
    def _over() -> list[BaseArtifact]:
        return [FolderArtifact(id="batches", path=Path("batches_in"))]

    @staticmethod
    def _outputs() -> list[BaseArtifact]:
        return [FolderArtifact(id="scored", path=Path("scored_out"))]

    def _map(self, **kwargs: object) -> MapTask:
        base: dict[str, object] = {
            "id": "score",
            "name": "score",
            "over": "batches",
            "item_input": "item",
            "inputs": self._over(),
            "outputs": self._outputs(),
            "task": _template_task(),
        }
        return MapTask(**{**base, **kwargs})  # type: ignore[arg-type]

    def test_non_iterable_over_requires_fan_out(self) -> None:
        """A plain file over-input is rejected unless fan_out produces the
        collection.
        """
        with pytest.raises(ValidationError, match="cannot be iterated"):
            self._map(
                inputs=[FileArtifact(id="batches", path=Path("b.txt"))],
                over="batches",
            )

    def test_fan_out_must_be_iterable(self) -> None:
        """A fan_out of a non-iterable kind is rejected."""
        with pytest.raises(ValidationError, match="'fan_out'"):
            self._map(fan_out=FileArtifact(id="mid", path=Path("m.txt")))

    def test_fan_out_id_may_not_collide_with_inputs(self) -> None:
        """The intermediate's id must not shadow any port."""
        with pytest.raises(ValidationError, match="collides"):
            self._map(
                fan_out=JSONArtifact(id="batches", path=Path("batches.json")),
            )

    def test_fan_out_id_may_not_collide_with_outputs(self) -> None:
        """The same collision rule covers outputs."""
        with pytest.raises(ValidationError, match="collides"):
            self._map(
                fan_out=JSONArtifact(id="scored", path=Path("s.json")),
            )

    def test_gather_reserves_the_slots_input_id(self) -> None:
        """No author-declared input may use the reserved id 'slots'."""
        with pytest.raises(ValidationError, match="reserved"):
            self._map(
                gather=CommandRuntime(command="cat $slots > $scored"),
                inputs=[
                    *self._over(),
                    FileArtifact(id="slots", path=Path("s.txt")),
                ],
            )

    def test_gather_allows_any_single_output_kind(self) -> None:
        """With gather the sole output may be a file instead of a folder;
        without it, the folder rule still holds.
        """
        gathered = self._map(
            outputs=[FileArtifact(id="merged", path=Path("merged.txt"))],
            gather=CommandRuntime(command="cat $slots > $merged"),
        )
        assert isinstance(gathered.outputs[0], FileArtifact)

        with pytest.raises(ValidationError, match="exactly one output"):
            self._map(
                outputs=[
                    FileArtifact(id="a", path=Path("a.txt")),
                    FileArtifact(id="b", path=Path("b.txt")),
                ],
                gather=CommandRuntime(command="true"),
            )

    def test_gather_runtime_must_satisfy_executor_runtimes(self) -> None:
        """A python gather behind a shell-only executor is loud at load
        time, not mid-run.
        """
        with pytest.raises(ValidationError, match="not compatible"):
            self._map(
                gather=PythonCodeStringRuntime(code="pass"),
            )


@pytest.mark.unit
class TestFingerprintSensitivity:
    """The fingerprint covers both transforms, so editing either one
    invalidates memoization like editing the inner task does.
    """

    async def _config_hash(self, **kwargs: object) -> str:
        kwargs.setdefault(
            "fan_out", JSONArtifact(id="intermediate", path=Path("items.json"))
        )
        kwargs.setdefault(
            "gather", CommandRuntime(command="cat $slots/* > $merged")
        )
        map_task = MapTask(
            id="score",
            name="score",
            over="batches",
            item_input="item",
            inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
            outputs=[FileArtifact(id="merged", path=Path("merged.txt"))],
            task=_template_task(),
            **kwargs,  # type: ignore[arg-type]
        )
        return (await map_task._fingerprint()).config_hash

    async def test_identical_transform_configs_hash_equal(
        self,
    ) -> None:
        """Same fan_out and gather, same hash."""
        assert await self._config_hash() == await self._config_hash()

    async def test_editing_fan_out_changes_the_hash(self) -> None:
        """Moving the intermediate's declared path changes the hash."""
        assert await self._config_hash() != await self._config_hash(
            fan_out=JSONArtifact(id="intermediate", path=Path("other.json"))
        )

    async def test_editing_gather_changes_the_hash(self) -> None:
        """Editing the gather command changes the hash."""
        assert await self._config_hash() != await self._config_hash(
            gather=CommandRuntime(command="tar czf $merged $slots")
        )

    async def test_adding_either_transform_changes_the_hash(self) -> None:
        """Absent transforms hash differently from present ones."""
        plain = MapTask(
            id="score",
            name="score",
            over="batches",
            item_input="item",
            inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
            outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
            task=_template_task(),
        )
        transformed = await self._config_hash()

        assert (await plain._fingerprint()).config_hash != transformed
