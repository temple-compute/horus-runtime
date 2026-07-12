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
Unit tests for the declarative map / fan-out / fan-in construct: MapOver,
MapExpander, the ``map:`` YAML lowering hook, and the ``wf.map(...)``
Python builder.
"""

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.artifact.json import JSONArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.dag import build_dependencies
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_builtin.workflow.map import (
    MapConfigurationError,
    MapExpander,
    MapOver,
    lower_map_entry,
    map_task,
)
from horus_runtime.context import HorusContext
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow


def _template_task(
    *,
    item_id: str = "batch",
    output_id: str = "scored",
    command: str = ("mkdir -p $scored && cp $batch/data.txt $scored/out.txt"),
) -> HorusTask:
    """A minimal, trivial-command template task for a collection map."""
    return HorusTask(
        id="template",
        name="template",
        runtime=CommandRuntime(command=command),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[FolderArtifact(id=item_id, path=Path("batch_in"))],
        outputs=[FolderArtifact(id=output_id, path=Path("scored_out"))],
    )


def _range_template_task(
    *,
    index_id: str = "idx",
    output_id: str = "scored",
    command: str = "mkdir -p $scored && cp $idx $scored/idx.json",
) -> HorusTask:
    """A minimal, trivial-command template task for a range map."""
    return HorusTask(
        id="template",
        name="template",
        runtime=CommandRuntime(command=command),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[FileArtifact(id=index_id, path=Path("idx_in"))],
        outputs=[FolderArtifact(id=output_id, path=Path("scored_out"))],
    )


def _gather_task(tmp_path: Path, *, input_id: str = "results") -> HorusTask:
    """A minimal gather task with a single FolderArtifact fan-in input."""
    return HorusTask(
        id="gather",
        name="gather",
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[FolderArtifact(id=input_id, path=Path("gather_in"))],
        outputs=[FileArtifact(id="done", path=tmp_path / "done.txt")],
    )


def _split_task(tmp_path: Path, names: list[str]) -> HorusTask:
    """A task whose FolderArtifact output already has *names* children."""
    batches = tmp_path / "batches"
    for name in names:
        child = batches / name
        child.mkdir(parents=True, exist_ok=True)
        (child / "data.txt").write_text(name)
    return HorusTask(
        id="split",
        name="split",
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        outputs=[FolderArtifact(id="batches", path=batches)],
    )


def _json_split_task(tmp_path: Path, items: list[object]) -> HorusTask:
    """A task whose JSON list output already contains *items*."""
    path = tmp_path / "batches.json"
    artifact = JSONArtifact(id="batches", path=path)
    artifact.write(items)
    return HorusTask(
        id="split",
        name="split",
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        outputs=[artifact],
    )


@pytest.mark.unit
class TestMapOver:
    """MapOver enforces collection-mode xor range-mode."""

    def test_collection_mode_valid(self) -> None:
        """All three collection fields together, no range: valid."""
        over = MapOver(
            source_task="split", source_output="batches", item_input="batch"
        )
        assert not over.is_range

    def test_range_mode_valid(self) -> None:
        """Range alone, no collection fields: valid."""
        over = MapOver(range=3, index_input="idx")
        assert over.is_range

    def test_range_with_collection_fields_raises(self) -> None:
        """Range combined with any collection field is rejected."""
        with pytest.raises(ValueError, match="cannot combine"):
            MapOver(source_task="split", range=3)

    def test_partial_collection_fields_raises(self) -> None:
        """Only some collection fields set, no range: rejected."""
        with pytest.raises(ValueError, match="requires"):
            MapOver(source_task="split", source_output="batches")

    def test_neither_mode_raises(self) -> None:
        """Neither range nor collection fields set: rejected."""
        with pytest.raises(ValueError, match="requires"):
            MapOver()


@pytest.mark.unit
class TestCollectionMapEndToEnd:
    """Fan-out over a FolderArtifact collection, then fan-in."""

    async def test_three_item_folder_fans_out_and_gathers(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        3-item folder source -> 3 deterministically-id'd clones, each
        writing its sliced output; gather runs after all three and its
        ``.gathered/`` folder has subdirs 0/1/2.
        """
        del horus_context
        split = _split_task(tmp_path, ["a", "b", "c"])
        gather = _gather_task(tmp_path)

        wf = HorusWorkflow(
            name="wf",
            tasks=[split, gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.map(
            id="score",
            template=_template_task(),
            over=("split", "batches", "batch"),
            gather=("gather", "results"),
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        clone_ids = [t.id for t in wf.tasks if t.id.startswith("score[")]
        assert clone_ids == ["score[0]", "score[1]", "score[2]"]
        for task in wf.tasks:
            assert task.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)

        gathered = tmp_path / "score.gathered"
        assert sorted(p.name for p in gathered.iterdir()) == [
            "0",
            "1",
            "2",
        ]
        for i in range(3):
            out = gathered / str(i) / "out.txt"
            assert out.exists()

    async def test_json_list_source_fans_out(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A JSON-list collection fans out one clone per element, in
        list order.
        """
        del horus_context
        split = _json_split_task(tmp_path, ["x", "y"])
        gather = _gather_task(tmp_path)

        wf = HorusWorkflow(
            name="wf",
            tasks=[split, gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        template = HorusTask(
            id="template",
            name="template",
            runtime=CommandRuntime(
                command="mkdir -p $scored && cp $item $scored/item.json"
            ),
            executor=ShellExecutor(),
            target=LocalTarget(),
            inputs=[FileArtifact(id="item", path=Path("item_in"))],
            outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
        )
        wf.map(
            id="score",
            template=template,
            over=("split", "batches", "item"),
            gather=("gather", "results"),
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        gathered = tmp_path / "score.gathered"
        assert sorted(p.name for p in gathered.iterdir()) == ["0", "1"]


@pytest.mark.unit
class TestRangeMapEndToEnd:
    """Fan-out over an integer range, with no upstream source task."""

    async def test_range_fans_out_and_gathers(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """range=3 with no source task -> 3 clones, gathered 0/1/2."""
        del horus_context
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(
            name="wf",
            tasks=[gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.map(
            id="rmap",
            template=_range_template_task(),
            range=3,
            index_input="idx",
            gather=("gather", "results"),
        )

        await wf.run(trigger_id="rmap")

        assert wf.status.value == "completed"
        clone_ids = sorted(t.id for t in wf.tasks if t.id.startswith("rmap["))
        assert clone_ids == ["rmap[0]", "rmap[1]", "rmap[2]"]
        gathered = tmp_path / "rmap.gathered"
        assert sorted(p.name for p in gathered.iterdir()) == [
            "0",
            "1",
            "2",
        ]


@pytest.mark.unit
class TestPartialCompletion:
    """A partially completed map resumes: done clones are skipped."""

    async def test_pre_created_clone_output_is_skipped(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Clone 1's output already exists on disk (as if from a prior run);
        re-running with a *fresh* workflow object re-derives the same
        clone set but skips clone 1 while the others run.
        """
        del horus_context
        # Pre-create clone 1's deterministic output location.
        gathered_1 = tmp_path / "score.gathered" / "1"
        gathered_1.mkdir(parents=True)
        (gathered_1 / "out.txt").write_text("already done")

        split = _split_task(tmp_path, ["a", "b", "c"])
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(
            name="wf",
            tasks=[split, gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.map(
            id="score",
            template=_template_task(),
            over=("split", "batches", "batch"),
            gather=("gather", "results"),
        )

        await wf.run(trigger_id="split")

        statuses = {t.id: t.status for t in wf.tasks}
        assert statuses["score[0]"] == TaskStatus.COMPLETED
        assert statuses["score[1]"] == TaskStatus.SKIPPED
        assert statuses["score[2]"] == TaskStatus.COMPLETED
        assert wf.status.value == "completed"


@pytest.mark.unit
class TestFanInOrdering:
    """Gather does not start until every clone has completed."""

    async def test_gather_waits_for_all_clones(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        While the (deliberately slow) clones are still running, gather is
        still IDLE; once the run finishes, gather has run after them.
        """
        del horus_context
        split = _split_task(tmp_path, ["a", "b"])
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(
            name="wf",
            tasks=[split, gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        slow_template = _template_task(
            command=(
                "sleep 0.3 && mkdir -p $scored && "
                "cp $batch/data.txt $scored/out.txt"
            )
        )
        wf.map(
            id="score",
            template=slow_template,
            over=("split", "batches", "batch"),
            gather=("gather", "results"),
        )

        run = asyncio.create_task(wf.run(trigger_id="split"))
        await asyncio.sleep(0.1)

        # Clones should be under way but gather must not have started yet.
        gather_task = next(t for t in wf.tasks if t.id == "gather")
        status_before: TaskStatus = gather_task.status
        assert status_before == TaskStatus.IDLE

        await run

        status_after: TaskStatus = gather_task.status
        assert status_after == TaskStatus.COMPLETED
        clone_statuses = [
            t.status for t in wf.tasks if t.id.startswith("score[")
        ]
        assert all(s == TaskStatus.COMPLETED for s in clone_statuses)

    async def test_clone_to_gather_edges_are_real_dependencies(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The clone -> gather edges created by ``expand()`` are ordinary
        task-to-task dependencies (not just decorative), so the
        scheduler's dependency graph makes gather depend on every clone
        once the map has run.
        """
        del horus_context
        split = _split_task(tmp_path, ["a", "b"])
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(
            name="wf",
            tasks=[split, gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.map(
            id="score",
            template=_template_task(),
            over=("split", "batches", "batch"),
            gather=("gather", "results"),
        )

        await wf.run(trigger_id="split")

        deps = build_dependencies(wf.tasks, wf.edges)
        clone_ids = {t.id for t in wf.tasks if t.id.startswith("score[")}
        assert clone_ids
        assert clone_ids <= deps["gather"]


@pytest.mark.unit
class TestYamlLowering:
    """The ``map:`` YAML block lowers to a map_expander task + edge."""

    def test_lower_collection_entry(self) -> None:
        """Collection-mode ``map:`` lowers to a map_expander dict with an
        input placeholder and a construction-time ordering edge.
        """
        entry = {
            "id": "score",
            "map": {
                "over": {
                    "source_task": "split",
                    "source_output": "batches",
                    "item_input": "batch",
                },
                "template": {"kind": "horus_task"},
                "gather": {"task": "gather", "input": "results"},
            },
        }
        expander, edges = lower_map_entry(entry)

        assert expander["kind"] == "map_expander"
        assert expander["id"] == "score"
        assert expander["over"]["source_task"] == "split"
        assert expander["over"]["range"] is None
        assert expander["gather_task"] == "gather"
        assert expander["gather_input"] == "results"
        assert len(expander["inputs"]) == 1
        assert expander["inputs"][0]["id"] == "batches"

        assert edges == [
            {
                "source": "split",
                "source_output": "batches",
                "target": "score",
                "target_input": "batches",
                "transfer": False,
            }
        ]

    def test_lower_range_entry(self) -> None:
        """Range-mode ``map:`` lowers with no construction-time edge and no
        placeholder input.
        """
        entry = {
            "id": "rmap",
            "map": {
                "range": 5,
                "index_input": "idx",
                "template": {"kind": "horus_task"},
                "gather": {"task": "gather", "input": "results"},
            },
        }
        expander, edges = lower_map_entry(entry)

        assert expander["over"]["range"] == 5
        assert expander["over"]["index_input"] == "idx"
        assert expander["inputs"] == []
        assert edges == []

    def test_yaml_workflow_loads_and_runs(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A full YAML workflow with a ``map:`` block loads via
        BaseWorkflow.from_yaml and runs to completion.
        """
        del horus_context
        batches = tmp_path / "batches"
        for name in ("a", "b"):
            (batches / name).mkdir(parents=True)
            (batches / name / "data.txt").write_text(name)

        wf_yaml = {
            "name": "map_yaml",
            "kind": "horus_workflow",
            "tasks": [
                {
                    "id": "split",
                    "name": "split",
                    "kind": "horus_task",
                    "runtime": {"kind": "command", "command": "true"},
                    "executor": {"kind": "shell"},
                    "target": {"kind": "local"},
                    "outputs": [
                        {
                            "id": "batches",
                            "kind": "folder",
                            "path": str(batches),
                        }
                    ],
                },
                {
                    "id": "score",
                    "map": {
                        "over": {
                            "source_task": "split",
                            "source_output": "batches",
                            "item_input": "batch",
                        },
                        "template": {
                            "kind": "horus_task",
                            "runtime": {
                                "kind": "command",
                                "command": (
                                    "mkdir -p $scored && "
                                    "cp $batch/data.txt $scored/out.txt"
                                ),
                            },
                            "executor": {"kind": "shell"},
                            "target": {"kind": "local"},
                            "inputs": [
                                {
                                    "id": "batch",
                                    "kind": "folder",
                                    "path": "batch_in",
                                }
                            ],
                            "outputs": [
                                {
                                    "id": "scored",
                                    "kind": "folder",
                                    "path": "scored_out",
                                }
                            ],
                        },
                        "gather": {"task": "gather", "input": "results"},
                    },
                },
                {
                    "id": "gather",
                    "name": "gather",
                    "kind": "horus_task",
                    "runtime": {"kind": "command", "command": "true"},
                    "executor": {"kind": "shell"},
                    "target": {"kind": "local"},
                    "inputs": [
                        {
                            "id": "results",
                            "kind": "folder",
                            "path": "gather_in",
                        }
                    ],
                    "outputs": [
                        {
                            "id": "done",
                            "kind": "file",
                            "path": "done.txt",
                        }
                    ],
                },
            ],
        }
        wf_path = tmp_path / "wf.yaml"
        with wf_path.open("w") as fh:
            yaml.safe_dump(wf_yaml, fh)

        wf = BaseWorkflow.from_yaml(wf_path)
        assert isinstance(wf, HorusWorkflow)
        score = next(t for t in wf.tasks if t.id == "score")
        assert isinstance(score, MapExpander)
        wf.orchestrator_target = LocalTarget(
            working_directory=tmp_path.as_posix()
        )

        asyncio.run(wf.run(trigger_id="split"))

        assert wf.status.value == "completed"
        gathered = tmp_path / "score.gathered"
        assert sorted(p.name for p in gathered.iterdir()) == ["0", "1"]

    def test_yaml_to_yaml_round_trip(self, tmp_path: Path) -> None:
        """to_yaml -> from_yaml round-trips a map workflow: the second
        load sees the already-lowered map_expander natively (no ``map:``
        block survives the dump), with the same task ids/kinds.
        """
        split = _split_task(tmp_path, ["a", "b"])
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(
            name="wf",
            tasks=[split, gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.map(
            id="score",
            template=_template_task(),
            over=("split", "batches", "batch"),
            gather=("gather", "results"),
        )

        out_path = tmp_path / "dump.yaml"
        wf.to_yaml(out_path)

        dumped = yaml.safe_load(out_path.read_text())
        score_dict = next(t for t in dumped["tasks"] if t["id"] == "score")
        assert "map" not in score_dict
        assert score_dict["kind"] == "map_expander"

        wf2 = BaseWorkflow.from_yaml(out_path)
        assert isinstance(wf2, HorusWorkflow)
        assert {t.id for t in wf2.tasks} == {"split", "score", "gather"}
        score2 = next(t for t in wf2.tasks if t.id == "score")
        assert isinstance(score2, MapExpander)
        assert score2.over.source_task == "split"
        assert score2.gather_task == "gather"
        assert score2.gather_input == "results"


@pytest.mark.unit
class TestPythonBuilderParity:
    """wf.map(...) and the equivalent YAML map: block agree structurally."""

    def test_python_builder_matches_yaml_lowering_shape(
        self, tmp_path: Path
    ) -> None:
        """Building the same map via wf.map(...) and via lower_map_entry
        yields the same over/gather wiring and construction-time edge.
        """
        split = _split_task(tmp_path, ["a", "b"])
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(name="wf", tasks=[split, gather])

        expander = wf.map(
            id="score",
            template=_template_task(),
            over=("split", "batches", "batch"),
            gather=("gather", "results"),
        )

        entry = {
            "id": "score",
            "map": {
                "over": {
                    "source_task": "split",
                    "source_output": "batches",
                    "item_input": "batch",
                },
                "template": {"kind": "horus_task"},
                "gather": {"task": "gather", "input": "results"},
            },
        }
        yaml_expander, yaml_edges = lower_map_entry(entry)
        yaml_over = yaml_expander["over"]

        assert expander.over.source_task == yaml_over["source_task"]
        assert expander.over.source_output == yaml_over["source_output"]
        assert expander.over.item_input == yaml_over["item_input"]
        assert expander.gather_task == yaml_expander["gather_task"]
        assert expander.gather_input == yaml_expander["gather_input"]

        construction_edge = next(e for e in wf.edges if e.target == "score")
        assert (construction_edge.source, construction_edge.source_output) == (
            yaml_edges[0]["source"],
            yaml_edges[0]["source_output"],
        )
        assert construction_edge.transfer is False
        assert yaml_edges[0]["transfer"] is False

    def test_map_task_requires_exactly_one_of_over_or_range(self) -> None:
        """Neither over nor range: rejected."""
        wf = HorusWorkflow(name="wf")
        with pytest.raises(MapConfigurationError):
            map_task(
                wf,
                id="score",
                template=_template_task(),
                gather=("gather", "results"),
            )

    def test_map_task_rejects_both_over_and_range(self) -> None:
        """Both over and range: rejected."""
        wf = HorusWorkflow(name="wf")
        with pytest.raises(MapConfigurationError):
            map_task(
                wf,
                id="score",
                template=_template_task(),
                over=("split", "batches", "batch"),
                range=3,
                gather=("gather", "results"),
            )


@pytest.mark.unit
class TestMapExpanderErrors:
    """MapExpander._run raises clear errors for common misconfigurations."""

    async def test_missing_orchestrator_target_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        No orchestrator_target set: a range map (no source-collection
        input, so the generic root-input transfer check never fires first)
        raises MapConfigurationError from the expander's own guard.
        """
        del horus_context
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(name="wf", tasks=[gather])
        wf.orchestrator_target = None
        wf.map(
            id="rmap",
            template=_range_template_task(),
            range=3,
            index_input="idx",
            gather=("gather", "results"),
        )
        with pytest.raises(MapConfigurationError):
            await wf.run(trigger_id="rmap")

    async def test_unknown_source_task_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """over.source_task not present in the workflow: raises."""
        del horus_context
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(
            name="wf",
            tasks=[gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        expander = MapExpander(
            id="score",
            name="score",
            over=MapOver(
                source_task="missing",
                source_output="batches",
                item_input="batch",
            ),
            template=_template_task().model_dump(mode="json"),
            gather_task="gather",
            gather_input="results",
            inputs=[FolderArtifact(id="batches", path=Path("marker"))],
        )
        wf.tasks.append(expander)
        with pytest.raises(MapConfigurationError):
            await wf.run(trigger_id="score")

    async def test_missing_gather_task_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """gather_task not present in the workflow: raises."""
        del horus_context
        split = _split_task(tmp_path, ["a"])
        wf = HorusWorkflow(
            name="wf",
            tasks=[split],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.map(
            id="score",
            template=_template_task(),
            over=("split", "batches", "batch"),
            gather=("missing_gather", "results"),
        )
        with pytest.raises(MapConfigurationError):
            await wf.run(trigger_id="split")

    async def test_template_with_wrong_output_count_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A template declaring zero or multiple outputs is rejected once
        clones are being built.
        """
        del horus_context
        split = _split_task(tmp_path, ["a"])
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(
            name="wf",
            tasks=[split, gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        bad_template = HorusTask(
            id="template",
            name="template",
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            inputs=[FolderArtifact(id="batch", path=Path("batch_in"))],
            outputs=[],
        )
        wf.map(
            id="score",
            template=bad_template,
            over=("split", "batches", "batch"),
            gather=("gather", "results"),
        )
        with pytest.raises(MapConfigurationError):
            await wf.run(trigger_id="split")

    async def test_non_list_json_source_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A JSON source that isn't a list is rejected."""
        del horus_context
        artifact = JSONArtifact(id="batches", path=tmp_path / "b.json")
        artifact.write({"not": "a list"})
        split = HorusTask(
            id="split",
            name="split",
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            outputs=[artifact],
        )
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(
            name="wf",
            tasks=[split, gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        template = HorusTask(
            id="template",
            name="template",
            runtime=CommandRuntime(command="true"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            inputs=[FileArtifact(id="item", path=Path("item_in"))],
            outputs=[FolderArtifact(id="scored", path=Path("scored_out"))],
        )
        wf.map(
            id="score",
            template=template,
            over=("split", "batches", "item"),
            gather=("gather", "results"),
        )
        with pytest.raises(MapConfigurationError):
            await wf.run(trigger_id="split")


@pytest.mark.unit
class TestCopyFolderCrossFilesystem:
    """MapExpander._copy_folder's tar pack/unpack branch (cross-target)."""

    async def test_cross_filesystem_copy_round_trips_contents(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Two targets with different location_id still get a correct
        copy, via the tar pack/unpack fallback.
        """
        del horus_context
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "file.txt").write_text("hello")
        dst_dir = tmp_path / "dst"

        class _OtherTarget(LocalTarget):
            add_to_registry = False

            @property
            def location_id(self) -> str:
                return "other://location"

        src_target = LocalTarget(working_directory=str(tmp_path))
        dst_target = _OtherTarget(working_directory=str(tmp_path))

        await MapExpander._copy_folder(
            src_target, str(src_dir), dst_target, dst_dir
        )

        assert (dst_dir / "file.txt").read_text() == "hello"

        # Re-copying (rmtree + fresh copytree) stays correct too.
        shutil.rmtree(dst_dir)
        await MapExpander._copy_folder(
            src_target, str(src_dir), dst_target, dst_dir
        )
        assert (dst_dir / "file.txt").read_text() == "hello"


@pytest.mark.unit
class TestMapExpanderReset:
    """MapExpander._reset clears the run counter and the (never-written)
    wiring marker.
    """

    async def test_reset_clears_runs(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """After a run, reset() sets runs back to 0 and status to IDLE."""
        del horus_context
        split = _split_task(tmp_path, ["a"])
        gather = _gather_task(tmp_path)
        wf = HorusWorkflow(
            name="wf",
            tasks=[split, gather],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.map(
            id="score",
            template=_template_task(),
            over=("split", "batches", "batch"),
            gather=("gather", "results"),
        )
        await wf.run(trigger_id="split")

        expander = next(t for t in wf.tasks if t.id == "score")
        assert expander.runs == 1
        await expander.reset()
        assert expander.runs == 0
        assert expander.status == TaskStatus.IDLE
