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
fan-out behaviour as an ordinary DAG node.
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
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_builtin.workflow.map import MapOver, MapTask
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.core.workflow.edge import WorkflowEdge

# The body sees the item under `over.as` (`batch`), the collection it came
# from still under its own id (`batches`), and the folder output id (`scored`)
# bound to its own slot directory.
_BODY = "cp $batch $scored/result.txt"


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
    command: str = _BODY,
    output_path: str = "scored_out",
    extra_inputs: list[FileArtifact] | None = None,
    max_concurrency: int | None = None,
) -> MapTask:
    """A ``horus_map`` task running *command* once per item."""
    return MapTask(
        id="score",
        name="score",
        over=MapOver(input_id=over_artifact.id, item_id="batch"),
        runtime=CommandRuntime(command=command),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[over_artifact, *(extra_inputs or [])],
        outputs=[FolderArtifact(id="scored", path=Path(output_path))],
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
                target_input=map_task.over.input_id,
            ),
            *(extra_edges or []),
        ],
        orchestrator_target=LocalTarget(working_directory=tmp_path.as_posix()),
    )


@pytest.mark.unit
class TestFolderFanOut:
    """Fan-out over a FolderArtifact source: one numbered slot per child,
    in sorted-by-name order.
    """

    async def test_three_files_fan_out_into_numbered_slots(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Each child file becomes its own slot, in name order."""
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["0", "1", "2"]
        for slot, name in enumerate(("a.txt", "b.txt", "c.txt")):
            assert (scored / str(slot) / "result.txt").read_text() == name

    async def test_downstream_task_consumes_the_folder_output(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The map's declared folder output is an ordinary output any
        downstream task can consume via a normal edge -- gathering the
        fan-out is just another node on the canvas.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
        )
        analysis = HorusTask(
            id="analysis",
            name="analysis",
            runtime=CommandRuntime(command="cat $scored/*/result.txt > $rep"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            inputs=[FolderArtifact(id="scored", path=Path("scored_in"))],
            outputs=[FileArtifact(id="rep", path=tmp_path / "report.txt")],
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
        assert (tmp_path / "report.txt").read_text() == "a.txtb.txt"

    async def test_clones_are_registered_as_ordinary_dag_tasks(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Clones land in ``wf.tasks`` (via ``expand()``), each with its own
        id, ordered after the map by an artifact-less edge, so a live
        dashboard, a workflow dump, or a resumed run all see them like any
        other task.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        clone_ids = {"score[0]", "score[1]"}
        clones = [t for t in wf.tasks if t.id in clone_ids]
        assert {c.id for c in clones} == clone_ids
        assert all(c.kind == "horus_task" for c in clones)
        assert all(c.status == TaskStatus.COMPLETED for c in clones)
        assert {
            (e.source, e.target) for e in wf.edges if e.target in clone_ids
        } == {("score", "score[0]"), ("score", "score[1]")}


@pytest.mark.unit
class TestCollectionStaysAddressable:
    """The item is a new artifact under ``over.as``, so the collection it
    came from is still addressable in the body under its own id.
    """

    async def test_body_reads_both_the_item_and_the_collection(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Every clone sees its own item and the whole collection."""
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            command=(
                "cp $batch $scored/result.txt && "
                "ls $batches | wc -l | tr -d ' ' > $scored/total.txt"
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        for slot, name in enumerate(("a.txt", "b.txt", "c.txt")):
            assert (scored / str(slot) / "result.txt").read_text() == name
            assert (scored / str(slot) / "total.txt").read_text() == "3\n"


@pytest.mark.unit
class TestListFanOut:
    """Fan-out over a JSON list source: one slot per element, zero-padded
    by index.
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
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["0", "1"]
        assert (scored / "0" / "result.txt").read_text() == '"x"'
        assert (scored / "1" / "result.txt").read_text() == '"y"'

    async def test_slot_index_is_zero_padded_to_the_widest_index(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """11 elements (max index 10, two digits) pad every slot to width 2,
        so the slots sort the same lexically and numerically.
        """
        del horus_context
        split = _json_split_task(tmp_path, [f"v{i}" for i in range(11)])
        map_task = _map_task(
            over_artifact=JSONArtifact(
                id="batches", path=Path("batches_in.json")
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        scored = tmp_path / "scored_out"
        slots = sorted(p.name for p in scored.iterdir())
        assert len(slots) == 11
        assert slots[0] == "00"
        assert slots[-1] == "10"
        assert (scored / "00" / "result.txt").read_text() == '"v0"'
        assert (scored / "10" / "result.txt").read_text() == '"v10"'


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
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            command="cat $batch $receptor > $scored/result.txt",
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
        for slot, name in enumerate(("a.txt", "b.txt")):
            content = (scored / str(slot) / "result.txt").read_text()
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
            )
            return _wire(tmp_path, split=split, map_task=map_task)

        await _build().run(trigger_id="split")

        scored = tmp_path / "scored_out"
        for slot in ("0", "2"):
            (scored / slot / "result.txt").write_text("STALE")
        shutil.rmtree(scored / "1")
        (tmp_path / ".horus" / "score[1].json").unlink()
        (tmp_path / ".horus" / "score.json").unlink()

        wf = _build()
        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert (scored / "0" / "result.txt").read_text() == "STALE"
        assert (scored / "2" / "result.txt").read_text() == "STALE"
        assert (scored / "1" / "result.txt").read_text() == "b.txt"

    async def test_an_unchanged_map_is_skipped_whole(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The map memoizes like any other task: with its own manifest
        intact and its inputs unchanged, it never re-expands at all.
        """
        del horus_context

        def _build() -> HorusWorkflow:
            split = _split_task(tmp_path, ["a.txt"])
            map_task = _map_task(
                over_artifact=FolderArtifact(
                    id="batches", path=Path("batches_in")
                ),
            )
            return _wire(tmp_path, split=split, map_task=map_task)

        await _build().run(trigger_id="split")

        wf = _build()
        await wf.run(trigger_id="split")

        score = next(t for t in wf.tasks if t.id == "score")
        assert score.status == TaskStatus.SKIPPED
        assert [t.id for t in wf.tasks if t.id.startswith("score[")] == []


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
            )
            return _wire(tmp_path, split=split, map_task=map_task)

        await _build().run(trigger_id="split")

        scored = tmp_path / "scored_out"
        for slot in ("0", "1"):
            (scored / slot / "result.txt").write_text("STALE")

        wf = _build()
        score = next(t for t in wf.tasks if t.id == "score")
        score.skip_if_complete = False
        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        for slot_to_use, name in enumerate(("a.txt", "b.txt")):
            assert (
                scored / str(slot_to_use) / "result.txt"
            ).read_text() == name


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
            command="exit 1",
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
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            command=(
                f"n=$(basename $batch); echo start-$n >> {log} && "
                f"sleep 0.05 && echo end-$n >> {log} && "
                "cp $batch $scored/result.txt"
            ),
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
        """``over.input_id`` must name one of this task's own inputs."""
        with pytest.raises(ValidationError, match="not found"):
            MapTask(
                id="score",
                name="score",
                over=MapOver(input_id="missing", item_id="batch"),
                runtime=CommandRuntime(command=_BODY),
                executor=ShellExecutor(),
                target=LocalTarget(),
                inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
                outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
            )

    def test_over_names_a_non_iterable_input(self) -> None:
        """``over.input_id`` must name an input that can enumerate itself."""
        with pytest.raises(ValidationError, match="not iterable"):
            MapTask(
                id="score",
                name="score",
                over=MapOver(input_id="batches", item_id="batch"),
                runtime=CommandRuntime(command=_BODY),
                executor=ShellExecutor(),
                target=LocalTarget(),
                inputs=[FileArtifact(id="batches", path=Path("batches_in"))],
                outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
            )

    def test_output_must_be_a_single_folder(self) -> None:
        """The one declared output is the folder the slots live in."""
        with pytest.raises(ValidationError, match="must be a folder"):
            MapTask(
                id="score",
                name="score",
                over=MapOver(input_id="batches", item_id="batch"),
                runtime=CommandRuntime(command=_BODY),
                executor=ShellExecutor(),
                target=LocalTarget(),
                inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
                outputs=[FileArtifact(id="scored", path=Path("scored.txt"))],
            )

    def test_item_id_collides_with_a_declared_port(self) -> None:
        """``over.as`` names a new artifact, so it must be a free id."""
        with pytest.raises(ValidationError, match="already the id"):
            MapTask(
                id="score",
                name="score",
                over=MapOver(input_id="batches", item_id="batches"),
                runtime=CommandRuntime(command=_BODY),
                executor=ShellExecutor(),
                target=LocalTarget(),
                inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
                outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
            )

    def test_exactly_one_output(self) -> None:
        """Two outputs have no unambiguous slot root."""
        with pytest.raises(ValidationError, match="exactly one output"):
            MapTask(
                id="score",
                name="score",
                over=MapOver(input_id="batches", item_id="batch"),
                runtime=CommandRuntime(command=_BODY),
                executor=ShellExecutor(),
                target=LocalTarget(),
                inputs=[FolderArtifact(id="batches", path=Path("batches_in"))],
                outputs=[
                    FolderArtifact(id="scored", path=Path("scored_out")),
                    FolderArtifact(id="other", path=Path("other_out")),
                ],
            )


@pytest.mark.unit
class TestRoundTrip:
    """A map is an ordinary task document: to_yaml/from_yaml preserves it
    and the reloaded workflow reruns cleanly.
    """

    async def test_yaml_round_trip_reruns(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Dumped after a run, reloaded, and re-run (skipped via its
        manifest, since nothing changed).
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)
        await wf.run(trigger_id="split")
        assert wf.status.value == "completed"

        out_path = tmp_path / "dump.yaml"
        wf.to_yaml(out_path)

        dumped = yaml.safe_load(out_path.read_text())
        score_dict = next(t for t in dumped["tasks"] if t["id"] == "score")
        assert score_dict["kind"] == "horus_map"
        assert score_dict["over"]["input_id"] == "batches"
        assert score_dict["runtime"]["command"] == _BODY

        wf2 = BaseWorkflow.from_yaml(out_path)
        assert isinstance(wf2, HorusWorkflow)
        score2 = next(t for t in wf2.tasks if t.id == "score")
        assert isinstance(score2, MapTask)
        assert score2.over.input_id == "batches"
        assert score2.over.item_id == "batch"

        await wf2.run(trigger_id="split")
        assert wf2.status.value == "completed"


@pytest.mark.unit
class TestCloneSideArtifacts:
    """Per-clone side-artifact attribution: each clone owns what it
    produced (its item, its log), and the map does not re-register any of
    it under its own id -- re-uploading the same artifact ids with a
    different task id would repoint the references and erase the per-clone
    attribution the UI's clone browser reads.
    """

    async def test_each_clone_registers_its_item_as_a_side_artifact(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The item a clone received is uploaded under the clone's own id,
        so what each run of the body got from the map is inspectable.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        for slot, name in enumerate(("a.txt", "b.txt")):
            clone = next(t for t in wf.tasks if t.id == f"score[{slot}]")
            item_id = f"score[{slot}]_item"
            items = [a for a in clone.side_artifacts if a.id == item_id]
            assert len(items) == 1
            assert items[0].path.name == name

    async def test_the_map_does_not_merge_clone_side_artifacts(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Nothing produced by a clone is re-registered under the map's
        id: the map's own side-artifact list stays free of `score[...]`
        entries, so the upload pass cannot repoint the clones' refs.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert not [
            a for a in map_task.side_artifacts if a.id.startswith("score[")
        ]
