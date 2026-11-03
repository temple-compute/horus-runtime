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

import asyncio
import contextvars
import hashlib
import itertools
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from pydantic import PrivateAttr, ValidationError

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.artifact.json import JSONArtifact
from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.executor.python_fn import PythonFunctionExecutor
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_builtin.workflow.map import (
    MapConfigurationError,
    MapOver,
    MapTask,
    _ItemStream,
)
from horus_builtin.workflow.scheduler import execute_task
from horus_runtime.context import HorusContext
from horus_runtime.core import lease as lease_module
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.exceptions import ArtifactDoesNotExistError
from horus_runtime.core.artifact.store import ArtifactStore
from horus_runtime.core.lease import BaseEvacuator
from horus_runtime.core.placement import ResourceCapacity
from horus_runtime.core.resources import ResourceRequest
from horus_runtime.core.target.channel import ChannelProcess
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.exceptions import (
    CommandFailedError,
    TargetLostError,
    TaskExecutionError,
)
from horus_runtime.core.task.status import (
    TERMINAL_STATUSES,
    SkipReason,
    TaskStatus,
)
from horus_runtime.core.transfer.events import TransferEvent
from horus_runtime.core.workflow.base import BaseWorkflow, EdgeSource
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import (
    ArtifactLostError,
    WorkflowExecutionError,
)
from horus_runtime.middleware.bound_task import (
    BoundTaskMiddleware,
    BoundTaskMiddlewareContext,
)

# The body sees the item under `over.as` (`batch`), the collection it came
# from still under its own id (`batches`), and the folder output id (`scored`)
# bound to its own slot directory.
_BODY = "cp $batch $scored/result.txt"


def _reads_everything(batch: Path, batches: Path) -> None:
    """A function body: artifacts arrive by parameter name, no ``$``."""
    del batch, batches


def _fails_long(batch: FileArtifact) -> None:
    """A function body failing with a long error for a "bad" item."""
    if batch.path.read_text() == "bad":
        raise ValueError("x" * 1000)


def _exits(batch: FileArtifact) -> None:
    """A function body that exits with N for an item holding "exit N"."""
    words = batch.path.read_text().split()
    if words[0] == "exit":
        raise CommandFailedError(
            f"Shell command exited with return code {words[1]}",
            int(words[1]),
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

    async def test_clone_hand_offs_emit_no_transfer_events(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Only the real split -> score edge reports a transfer; the map's
        per-clone item hand-offs are not edges and stay silent.
        """
        split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        transfers = [
            c.args[0]
            for c in emit.call_args_list
            if isinstance(c.args[0], TransferEvent)
        ]
        assert [(e.task_id, e.phase) for e in transfers] == [
            ("score", "started"),
            ("score", "finished"),
        ]

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

    def test_clones_start_without_the_maps_run_state(
        self, tmp_path: Path
    ) -> None:
        """A clone does not inherit the map's times or error."""
        map_task = _map_task(
            over_artifact=FolderArtifact(id="batches", path=tmp_path)
        )
        map_task.started_at = map_task.finished_at = datetime.now(UTC)
        map_task.error = "RuntimeError: boom"

        clone = map_task._clone(
            "0", FileArtifact(id="batches", path=tmp_path / "a"), tmp_path, {}
        )

        assert clone.started_at is None
        assert clone.finished_at is None
        assert clone.error is None


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

    async def test_hook_fires_for_the_map_and_each_clone(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The bound-task hook runs once for the map and once per clone."""
        del horus_context
        calls: list[str] = []
        original_registry = list(BoundTaskMiddleware.registry)

        split = _json_split_task(tmp_path, ["x", "y"])
        map_task = _map_task(
            over_artifact=JSONArtifact(
                id="batches", path=Path("batches_in.json")
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        class RecordingMiddleware(BoundTaskMiddleware):
            async def before(
                self, context: BoundTaskMiddlewareContext
            ) -> None:
                calls.append(context.task.id)

        try:
            await wf.run(trigger_id="split")
        finally:
            BoundTaskMiddleware.registry = original_registry

        assert wf.status.value == "completed"
        assert sorted(calls) == ["score", "score[0]", "score[1]", "split"]

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

    async def test_clones_reuse_the_maps_shared_input_digests(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The map hashes a shared input once; each clone hashes only its own
        item, and still records the fingerprint it would hash by itself.
        """
        del horus_context
        (tmp_path / "receptor.txt").write_text("RECEPTOR")

        def _build() -> HorusWorkflow:
            split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
            map_task = _map_task(
                over_artifact=FolderArtifact(
                    id="batches", path=Path("batches_in")
                ),
                command="cat $batch $receptor > $scored/result.txt",
                extra_inputs=[
                    FileArtifact(id="receptor", path=Path("receptor_in"))
                ],
            )
            return _wire(
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

        hashed: list[str] = []
        original = ArtifactStore.digest

        async def spy(
            self: ArtifactStore, artifact: BaseArtifact
        ) -> str | None:
            hashed.append(artifact.id)
            return await original(self, artifact)

        wf = _build()
        with patch.object(ArtifactStore, "digest", spy):
            await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert hashed.count("receptor") == 1
        assert hashed.count("batch") == 3
        clones = [t for t in wf.tasks if t.id.startswith("score[")]
        for clone in clones:
            assert isinstance(clone, HorusTask)
            assert await clone._read_manifest() == await clone._fingerprint()

        # A map that lost its manifest runs again: its clones' skip check
        # reuses the digests too.
        score = cast(MapTask, next(t for t in wf.tasks if t.id == "score"))
        Path(cast(str, score._manifest_path())).unlink()
        hashed.clear()
        wf = _build()
        with patch.object(ArtifactStore, "digest", spy):
            await wf.run(trigger_id="split")

        assert hashed.count("receptor") == 1
        assert hashed.count("batch") == 3
        clones = [t for t in wf.tasks if t.id.startswith("score[")]
        assert [t.status for t in clones] == [TaskStatus.SKIPPED] * 3


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

    async def test_changing_one_item_reruns_only_its_clone(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A clone fingerprints its own item, not the whole collection: when
        one item changes, the map re-expands but its siblings stay skipped
        (their poisoned outputs survive) and only the changed slot reruns.
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
        for slot in ("0", "1"):
            (scored / slot / "result.txt").write_text("STALE")
        wf = _build()
        # After _build, which seeds every file with its own name.
        (tmp_path / "batches" / "c.txt").write_text("changed")
        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert (scored / "0" / "result.txt").read_text() == "STALE"
        assert (scored / "1" / "result.txt").read_text() == "STALE"
        assert (scored / "2" / "result.txt").read_text() == "changed"

    def test_a_body_reading_the_collection_fingerprints_it(self) -> None:
        """A body that may read the whole collection depends on all of it, so
        its clones keep the collection in their fingerprint.
        """
        over = FolderArtifact(id="batches", path=Path("batches_in"))
        plain = _map_task(over_artifact=over)
        assert not plain._body_may_read("batches")
        for command in ("ls $batches > $scored/a", "echo ${task.inputs}"):
            named = _map_task(over_artifact=over, command=command)
            assert named._body_may_read("batches")
        function = _map_task(over_artifact=over)
        function.runtime = PythonFunctionRuntime(func=_reads_everything)
        assert function._body_may_read("batches")

    async def test_a_reloaded_clone_keeps_its_fingerprint_scope(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A clone reloaded from a dump (how tc-os reruns from a snapshot)
        still leaves the collection out, so it matches its own manifest.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(id="batches", path=Path("batches_in"))
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)
        await wf.run(trigger_id="split")
        dump = tmp_path / "dump.yaml"
        wf.to_yaml(dump)

        reloaded = BaseWorkflow.from_yaml(dump)
        clone = next(t for t in reloaded.tasks if t.id == "score[0]")
        assert clone.unfingerprinted_inputs == ["batches"]

    async def test_shrinking_map_drops_stale_clones_and_slots(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Reloaded from a dump that still carries a 3-item expansion (what
        tc-os does when re-running from a run) and re-run over 1 item, the
        map leaves exactly one clone, one clone edge, and one slot folder.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)
        await wf.run(trigger_id="split")
        dump = tmp_path / "dump.yaml"
        wf.to_yaml(dump)

        for name in ("b.txt", "c.txt"):
            (tmp_path / "batches" / name).unlink()
        (tmp_path / ".horus" / "score.json").unlink()

        wf2 = BaseWorkflow.from_yaml(dump)
        assert len([t for t in wf2.tasks if t.id.startswith("score[")]) == 3
        await wf2.run(trigger_id="split")

        assert wf2.status.value == "completed"
        assert [t.id for t in wf2.tasks if t.id.startswith("score[")] == [
            "score[0]"
        ]
        assert [e.target for e in wf2.edges if e.source == "score"] == [
            "score[0]"
        ]
        scored = tmp_path / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["0"]

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

    async def test_a_changed_upstream_folder_reruns_the_map(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The bug this guards: a folder input used to fingerprint as a
        constant, so after the upstream task rewrote the collection the map
        and every clone still skipped as complete, keeping stale results.
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

        wf = _build()
        # _split_task rewrites the original contents, so change them after.
        (tmp_path / "batches" / "a.txt").write_text("changed")
        await wf.run(trigger_id="split")

        score = next(t for t in wf.tasks if t.id == "score")
        assert score.status == TaskStatus.COMPLETED
        scored = tmp_path / "scored_out"
        assert (scored / "0" / "result.txt").read_text() == "changed"

    async def test_an_unchanged_clone_skips_before_transfer(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A map that lost its manifest (say, interrupted after its clones
        ran) runs again, but its unchanged clones skip without their inputs
        being transferred.
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

        first = _build()
        await first.run(trigger_id="split")
        stale = cast(MapTask, next(t for t in first.tasks if t.id == "score"))
        Path(cast(str, stale._manifest_path())).unlink()

        wf = _build()
        transferred: list[str] = []
        original = BaseWorkflow.transfer_artifacts

        async def spy(
            self: BaseWorkflow, task: HorusTask, *args: object
        ) -> None:
            transferred.append(task.id)
            await original(self, task, *args)  # type: ignore[arg-type]

        with patch.object(BaseWorkflow, "transfer_artifacts", spy):
            await wf.run(trigger_id="split")

        score = next(t for t in wf.tasks if t.id == "score")
        assert score.status == TaskStatus.COMPLETED
        clones = [t for t in wf.tasks if t.id.startswith("score[")]
        assert [t.status for t in clones] == [TaskStatus.SKIPPED] * 2
        assert not [t for t in transferred if t.startswith("score[")]


@pytest.mark.unit
class TestMapStatus:
    """The map's own status while its clones run."""

    async def test_map_is_running_while_its_clones_run(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Clones bypass the map's executor, the only place a task flips
        from CONFIGURING to RUNNING, so the map used to report
        "configuring" for its whole fan-out.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _map_task(
            over_artifact=FolderArtifact(id="batches", path=Path("batches_in"))
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)

        seen: list[TaskStatus] = []
        original = execute_task

        async def spy(*args: object, **kwargs: object) -> None:
            seen.append(map_task.status)
            await original(*args, **kwargs)  # type: ignore[arg-type]

        with patch("horus_builtin.workflow.map.execute_task", spy):
            await wf.run(trigger_id="split")

        assert seen == [TaskStatus.RUNNING, TaskStatus.RUNNING]
        assert map_task.status == TaskStatus.COMPLETED


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

    async def test_continue_policy_lets_sibling_clones_finish(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Under ``failure_policy="continue"`` one failing clone does not
        cancel its siblings, yet the map and the workflow still fail.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
        # b fails at once; a and c finish after it, so a fail-fast map would
        # have cancelled them.
        map_task = _map_task(
            over_artifact=FolderArtifact(
                id="batches", path=Path("batches_in")
            ),
            command=(
                '[ "$(basename $batch)" = b.txt ] && exit 1; '
                "sleep 0.2 && cp $batch $scored/result.txt"
            ),
        )
        wf = _wire(tmp_path, split=split, map_task=map_task)
        wf.failure_policy = "continue"

        with pytest.raises(WorkflowExecutionError):
            await wf.run(trigger_id="split")

        clones = {
            t.id: t.status for t in wf.tasks if t.id.startswith("score[")
        }
        assert clones == {
            "score[0]": TaskStatus.COMPLETED,
            "score[1]": TaskStatus.FAILED,
            "score[2]": TaskStatus.COMPLETED,
        }
        assert map_task.status == TaskStatus.FAILED
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


def _edge(
    source: str, output: str, target: str, input_id: str
) -> WorkflowEdge:
    """A transfer edge from *source*'s *output* into *target*'s input."""
    return WorkflowEdge(
        source=source,
        source_output=output,
        target=target,
        target_input=input_id,
    )


def _plain(task_id: str) -> HorusTask:
    """A plain task with one folder output, ``<task_id>_out``."""
    return HorusTask(
        id=task_id,
        name=task_id,
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        outputs=[FolderArtifact(id="out", path=Path(f"{task_id}_out"))],
    )


def _over_map(task_id: str, **kwargs: Any) -> MapTask:
    """A map over its ``batches`` input, writing ``<task_id>_out``."""
    return MapTask(
        id=task_id,
        name=task_id,
        over=MapOver(input_id="batches", item_id="batch"),
        runtime=CommandRuntime(command="cp $batch $scored/"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[FolderArtifact(id="batches", path=Path(f"{task_id}_in"))],
        outputs=[FolderArtifact(id="scored", path=Path(f"{task_id}_out"))],
        **kwargs,
    )


def _consumer(
    *,
    inputs: tuple[str, ...] = ("a_out",),
    command: str = "ls $item > $out/ls.txt",
    **kwargs: Any,
) -> MapTask:
    """Map ``b`` over its ``a_out`` input, one folder input per id."""
    return MapTask(
        id="b",
        name="b",
        over=MapOver(input_id="a_out", item_id="item"),
        runtime=CommandRuntime(command=command),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[FolderArtifact(id=i, path=Path(f"{i}_in")) for i in inputs],
        outputs=[FolderArtifact(id="out", path=Path("b_out"))],
        **kwargs,
    )


def _pairing_wf(
    b: MapTask, tasks: list[BaseTask], edges: list[WorkflowEdge]
) -> HorusWorkflow:
    """A workflow of *tasks* and *b*, wired by *edges*."""
    return HorusWorkflow(name="wf", tasks=[*tasks, b], edges=edges)


@pytest.mark.unit
class TestMapFields:
    """``chain``, ``zip`` and ``drop_exit_codes`` are checked at load."""

    def test_v1_zip_input_not_found(self) -> None:
        """A zip entry must name one of the map's own inputs."""
        with pytest.raises(
            ValidationError,
            match=re.escape(
                "Input artifact 'reads' not found in task inputs."
            ),
        ):
            _consumer(zip=[MapOver(input_id="reads", item_id="read")])

    def test_v2_zip_input_not_iterable(self) -> None:
        """A zip input must enumerate its items."""
        with pytest.raises(
            ValidationError,
            match=re.escape("Input artifact 'reads' is not iterable."),
        ):
            MapTask(
                id="b",
                name="b",
                over=MapOver(input_id="a_out", item_id="item"),
                runtime=CommandRuntime(command="true"),
                executor=ShellExecutor(),
                target=LocalTarget(),
                inputs=[
                    FolderArtifact(id="a_out", path=Path("a_out_in")),
                    FileArtifact(id="reads", path=Path("reads.txt")),
                ],
                outputs=[FolderArtifact(id="out", path=Path("b_out"))],
                zip=[MapOver(input_id="reads", item_id="read")],
            )

    @pytest.mark.parametrize(
        ("entry", "item"),
        [
            (MapOver(input_id="c_out", item_id="item"), "item"),
            (MapOver(input_id="c_out", item_id="c_out"), "c_out"),
            (MapOver(input_id="a_out", item_id="pair"), "a_out"),
        ],
    )
    def test_v3_zip_binding_collides(self, entry: MapOver, item: str) -> None:
        """A zip ``as`` is a free id, and no input is iterated twice."""
        with pytest.raises(
            ValidationError,
            match=re.escape(f"Map task 'b' binds '{item}' twice."),
        ):
            _consumer(inputs=("a_out", "c_out"), zip=[entry])

    def test_v4_chain_over_not_from_map(self) -> None:
        """A chained map's ``over`` must come from a map."""
        with pytest.raises(
            ValidationError,
            match=re.escape(
                "Map task 'b' is chained, but 'a_out' does not come from a "
                "map."
            ),
        ):
            _pairing_wf(
                _consumer(chain=True),
                [_plain("a")],
                [_edge("a", "out", "b", "a_out")],
            )

    def test_v5_zip_not_from_map(self) -> None:
        """A zip input must come from a map, not a plain task."""
        with pytest.raises(
            ValidationError,
            match=re.escape(
                "Map task 'b' pairs 'c_out' by item, but it does not come "
                "from a map."
            ),
        ):
            _pairing_wf(
                _consumer(
                    inputs=("a_out", "c_out"),
                    zip=[MapOver(input_id="c_out", item_id="pair")],
                ),
                [_plain("split"), _over_map("a"), _plain("c")],
                [
                    _edge("split", "out", "a", "batches"),
                    _edge("a", "scored", "b", "a_out"),
                    _edge("c", "out", "b", "c_out"),
                ],
            )

    def test_v6_zip_other_item_space(self) -> None:
        """Paired maps must iterate the same collection."""
        b = _consumer(
            inputs=("a_out", "c_out"),
            zip=[MapOver(input_id="c_out", item_id="pair")],
        )
        tasks: list[BaseTask] = [
            _plain("split"),
            _plain("split2"),
            _over_map("a"),
            _over_map("c"),
        ]
        edges = [
            _edge("split", "out", "a", "batches"),
            _edge("a", "scored", "b", "a_out"),
            _edge("c", "scored", "b", "c_out"),
        ]
        with pytest.raises(
            ValidationError,
            match=re.escape(
                "Map task 'b' pairs 'c_out' by item, but 'c' maps over "
                "different items than 'a'."
            ),
        ):
            _pairing_wf(
                b, tasks, [*edges, _edge("split2", "out", "c", "batches")]
            )

        # Over the same collection, the pairing loads.
        _pairing_wf(
            b.model_copy(deep=True),
            tasks,
            [*edges, _edge("split", "out", "c", "batches")],
        )

    def test_v7_chained_body_reads_whole_folder(self) -> None:
        """A chained command cannot name a streamed folder."""
        with pytest.raises(
            ValidationError,
            match=re.escape(
                "Map task 'b' is chained, so it cannot read all of 'c_out'. "
                "Use '$pair' for this item."
            ),
        ):
            _consumer(
                inputs=("a_out", "c_out"),
                command="cp ${c_out.path}/x $out/",
                chain=True,
                zip=[MapOver(input_id="c_out", item_id="pair")],
            )
        # `$$` is a literal dollar, not a placeholder.
        _consumer(command="echo $$a_out > $out/x", chain=True)

    def test_v7_task_placeholder(self) -> None:
        """``${task.inputs}`` reads every folder; ``${task.resources.*}``,
        ``${task.id}`` and ``${task.name}`` do not.
        """
        with pytest.raises(
            ValidationError,
            match=re.escape(
                "Map task 'b' is chained, so it cannot read all of "
                "'task.inputs'. Use '$item' for this item."
            ),
        ):
            _consumer(command="ls ${task.inputs}", chain=True)
        _consumer(
            command=(
                "run -t ${task.resources.cpus} ${task.id} ${task.name} $item"
            ),
            chain=True,
        )

    def test_v8_python_chain_loads(self) -> None:
        """A Python body cannot be checked, so a chain over it loads."""
        b = _consumer(chain=True)
        b.runtime = PythonFunctionRuntime(func=_reads_everything)
        b.executor = PythonFunctionExecutor()
        wf = _pairing_wf(
            MapTask.model_validate(b.model_dump()),
            [_plain("split"), _over_map("a")],
            [
                _edge("split", "out", "a", "batches"),
                _edge("a", "scored", "b", "a_out"),
            ],
        )
        loaded = next(t for t in wf.tasks if t.id == "b")
        assert isinstance(loaded, MapTask)
        assert loaded.chain

    def test_v9_second_edge_from_chained_source(self) -> None:
        """A chained source feeds nothing but its paired input."""
        with pytest.raises(
            ValidationError,
            match=re.escape(
                "Map task 'b' is chained to 'a', so no other input can take "
                "all of its output."
            ),
        ):
            _pairing_wf(
                _consumer(inputs=("a_out", "a_all"), chain=True),
                [_plain("split"), _over_map("a")],
                [
                    _edge("split", "out", "a", "batches"),
                    _edge("a", "scored", "b", "a_out"),
                    _edge("a", "scored", "b", "a_all"),
                ],
            )

    def test_drop_code_zero_rejected(self) -> None:
        """0 is success, never a drop."""
        with pytest.raises(
            ValidationError,
            match=re.escape(
                "Map task 'b' lists exit code 0 to drop items, but 0 means "
                "success."
            ),
        ):
            _consumer(drop_exit_codes=[3, 0])

    @pytest.mark.parametrize("code", [-1, 256])
    def test_drop_code_out_of_range_rejected(self, code: int) -> None:
        """A drop code is an exit code a command can return."""
        with pytest.raises(
            ValidationError,
            match=re.escape(
                f"Map task 'b' lists exit code {code} to drop items. Use a "
                "code from 1 to 255."
            ),
        ):
            _consumer(drop_exit_codes=[code])


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

    async def test_chain_zip_drop_codes_round_trip(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A chained map runs after its source like any other map, and the
        new fields survive to_yaml/from_yaml.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        a = _over_map("a")
        c = _over_map("c")
        b = _consumer(
            inputs=("a_out", "c_out"),
            chain=True,
            zip=[MapOver(input_id="c_out", item_id="pair")],
            drop_exit_codes=[3, 10],
        )
        wf = HorusWorkflow(
            name="wf",
            tasks=[split, a, c, b],
            edges=[
                _edge("split", "batches", "a", "batches"),
                _edge("split", "batches", "c", "batches"),
                _edge("a", "scored", "b", "a_out"),
                _edge("c", "scored", "b", "c_out"),
            ],
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        await wf.run(trigger_id="split")
        assert wf.status.value == "completed"
        assert sorted(p.name for p in (tmp_path / "b_out").iterdir()) == [
            "0",
            "1",
        ]

        out_path = tmp_path / "dump.yaml"
        wf.to_yaml(out_path)

        dumped = yaml.safe_load(out_path.read_text())
        b_dict = next(t for t in dumped["tasks"] if t["id"] == "b")
        assert b_dict["chain"] is True
        assert b_dict["drop_exit_codes"] == [3, 10]
        assert [z["input_id"] for z in b_dict["zip"]] == ["c_out"]

        wf2 = BaseWorkflow.from_yaml(out_path)
        b2 = next(t for t in wf2.tasks if t.id == "b")
        assert isinstance(b2, MapTask)
        assert b2.chain is True
        assert b2.zip == [MapOver(input_id="c_out", item_id="pair")]
        assert b2.drop_exit_codes == [3, 10]

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


# Fails (exit 137, as an OOM kill would) for any item whose content is
# "bad"; b.txt fails at once, the others finish after it, so a fail-fast map
# would have cancelled them.
_FLAKY = (
    'grep -q bad "$batch" && exit 137; '
    "sleep 0.2 && cp $batch $scored/result.txt"
)


def _dropping_map(**kwargs: object) -> MapTask:
    """A map over the split folder that drops failed items."""
    map_task = _map_task(
        over_artifact=FolderArtifact(id="batches", path=Path("batches_in")),
        **kwargs,  # type: ignore[arg-type]
    )
    map_task.on_item_failure = "drop"
    return map_task


def _drop_code_wf(
    tmp_path: Path, contents: dict[str, str], **kwargs: Any
) -> tuple[HorusWorkflow, MapTask]:
    """``split -> score``, the body exiting N for an item holding "exit N",
    with ``drop_exit_codes: [3]``.
    """
    split = _split_task(tmp_path, list(contents))
    for name, content in contents.items():
        (tmp_path / "batches" / name).write_text(content)
    map_task = _map_task(
        over_artifact=FolderArtifact(id="batches", path=Path("batches_in"))
    )
    map_task.runtime = PythonFunctionRuntime(func=_exits)
    map_task.executor = PythonFunctionExecutor()
    map_task.drop_exit_codes = [3]
    for key, value in kwargs.items():
        setattr(map_task, key, value)
    return _wire(tmp_path, split=split, map_task=map_task), map_task


def _drop_events(emit: MagicMock) -> list[HorusTaskEvent]:
    """The drop events among *emit*'s calls."""
    return [
        c.args[0]
        for c in emit.call_args_list
        if isinstance(c.args[0], HorusTaskEvent) and "map" in c.args[0].data
    ]


def _drop_messages(emit: MagicMock) -> list[str]:
    """The messages of the drop events among *emit*'s calls."""
    return [
        c.args[0].message
        for c in emit.call_args_list
        if isinstance(c.args[0], HorusTaskEvent)
        and c.args[0].message
        and "dropped" in c.args[0].message
    ]


def _report_map() -> MapTask:
    """A second map iterating the first map's (``score``) slots."""
    return MapTask(
        id="report",
        name="report",
        over=MapOver(input_id="scored_in", item_id="slot"),
        runtime=CommandRuntime(command="cp $slot/result.txt $reports/"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[FolderArtifact(id="scored_in", path=Path("scored_in"))],
        outputs=[FolderArtifact(id="reports", path=Path("reports_out"))],
        on_item_failure="drop",
    )


def _chain(tmp_path: Path, names: list[str]) -> HorusWorkflow:
    """The chain ``split -> score -> report``, both maps dropping."""
    report = _report_map()
    return _wire(
        tmp_path,
        split=_split_task(tmp_path, names),
        map_task=_dropping_map(command=_FLAKY),
        extra_tasks=[report],
        extra_edges=[
            WorkflowEdge(
                source="score",
                source_output="scored",
                target="report",
                target_input="scored_in",
            )
        ],
    )


@pytest.mark.unit
class TestDropFailedItems:
    """``on_item_failure: drop``: a failed clone is dropped from the map's
    output instead of failing the map.
    """

    async def test_a_failed_item_is_dropped_and_the_run_completes(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Under the default fail_fast policy: the siblings are not
        cancelled, the failed slot is removed and recorded, the clone stays
        FAILED, and the map and the run complete.
        """
        split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
        (tmp_path / "batches" / "b.txt").write_text("bad")
        map_task = _dropping_map(command=_FLAKY)
        wf = _wire(tmp_path, split=split, map_task=map_task)

        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert map_task.status == TaskStatus.COMPLETED
        clones = {
            t.id: t.status for t in wf.tasks if t.id.startswith("score[")
        }
        assert clones == {
            "score[0]": TaskStatus.COMPLETED,
            "score[1]": TaskStatus.FAILED,
            "score[2]": TaskStatus.COMPLETED,
        }
        scored = tmp_path / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["0", "2"]

        # Derived, not published: nothing about the drop is dumped.
        assert map_task._dropped_slots == ["1"]
        assert "_dropped_slots" not in map_task.model_dump()
        (message,) = _drop_messages(emit)
        assert "1 of 3" in message
        assert "1 (b.txt): " in message

    async def test_the_drop_event_is_capped(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A large drop lists 20 items, then "+N more", each error cut."""
        names = [f"{i:02d}.txt" for i in range(25)]
        split = _split_task(tmp_path, names)
        map_task = _dropping_map()
        map_task.runtime = PythonFunctionRuntime(func=_fails_long)
        map_task.executor = PythonFunctionExecutor()
        wf = _wire(tmp_path, split=split, map_task=map_task)
        for name in names[1:]:
            (tmp_path / "batches" / name).write_text("bad")

        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        assert map_task.status == TaskStatus.COMPLETED
        (message,) = _drop_messages(emit)
        lines = message.splitlines()
        assert len(lines) == 1 + 20 + 1
        assert lines[-1] == "+4 more"
        assert (
            lines[1] == "01 (01.txt): " + ("ValueError: " + "x" * 1000)[:500]
        )
        # The message is capped; `data` lists every drop.
        (event,) = _drop_events(emit)
        dropped = event.data["map"]["dropped"]
        assert [d["slot"] for d in dropped] == [
            f"{i:02d}" for i in range(1, 25)
        ]
        assert dropped[0]["item"] == "01.txt"
        assert dropped[0]["reason"] == ("ValueError: " + "x" * 1000)[:500]

    async def test_every_item_failing_fails_the_map(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Nothing left to hand downstream: the map and the run fail."""
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _dropping_map(command="exit 1")
        wf = _wire(tmp_path, split=split, map_task=map_task)

        with pytest.raises(Exception):  # noqa: B017
            await wf.run(trigger_id="split")

        assert map_task.status == TaskStatus.FAILED
        assert map_task.error is not None
        assert "All 2 items were dropped in score." in map_task.error
        assert wf.status.value == "failed"

    async def test_drop_code_drops_under_fail_policy(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A drop code drops the item even under ``on_item_failure:
        fail``: the slot is removed and the map and the run complete.
        """
        del horus_context
        wf, map_task = _drop_code_wf(
            tmp_path, {"a.txt": "a", "b.txt": "exit 3", "c.txt": "c"}
        )

        await wf.run(trigger_id="split")

        assert map_task.on_item_failure == "fail"
        assert wf.status.value == "completed"
        assert map_task.status == TaskStatus.COMPLETED
        assert map_task._dropped_slots == ["1"]
        scored = tmp_path / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["0", "2"]

    async def test_other_code_fails_the_map(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A code not in the list fails the map, as without drop codes."""
        del horus_context
        wf, map_task = _drop_code_wf(
            tmp_path, {"a.txt": "a", "b.txt": "exit 4"}
        )

        with pytest.raises(Exception):  # noqa: B017
            await wf.run(trigger_id="split")

        assert map_task.status == TaskStatus.FAILED
        assert wf.status.value == "failed"
        clone = next(t for t in wf.tasks if t.id == "score[1]")
        assert clone.status == TaskStatus.FAILED
        assert clone.skip_reason is None

    async def test_drop_code_under_fail_fast_keeps_siblings(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Under fail_fast, a drop in the first slot cancels no sibling."""
        del horus_context
        wf, map_task = _drop_code_wf(
            tmp_path, {"a.txt": "exit 3", "b.txt": "b", "c.txt": "c"}
        )

        await wf.run(trigger_id="split")

        assert wf.failure_policy == "fail_fast"
        assert map_task.status == TaskStatus.COMPLETED
        statuses = {
            t.id: t.status for t in wf.tasks if t.id.startswith("score[")
        }
        assert statuses == {
            "score[0]": TaskStatus.SKIPPED,
            "score[1]": TaskStatus.COMPLETED,
            "score[2]": TaskStatus.COMPLETED,
        }

    async def test_drop_code_never_retries(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A drop is final: the clone ran once."""
        del horus_context
        wf, _map = _drop_code_wf(tmp_path, {"a.txt": "a", "b.txt": "exit 3"})

        await wf.run(trigger_id="split")

        clone = next(t for t in wf.tasks if t.id == "score[1]")
        assert clone.runs == 1
        assert clone._target_loss_retries == 0

    async def test_dropped_clone_error_names_the_code(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """The clone's error says which code dropped it."""
        del horus_context
        wf, _map = _drop_code_wf(tmp_path, {"a.txt": "a", "b.txt": "exit 3"})

        await wf.run(trigger_id="split")

        clone = next(t for t in wf.tasks if t.id == "score[1]")
        assert clone.error == "Dropped: exit code 3"

    async def test_drop_code_clone_is_skipped_dropped(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A drop-code clone ends SKIPPED with ``dropped``; a crash under
        ``on_item_failure: drop`` stays FAILED.
        """
        del horus_context
        wf, _map = _drop_code_wf(
            tmp_path,
            {"a.txt": "a", "b.txt": "exit 3", "c.txt": "exit 4"},
            on_item_failure="drop",
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        tasks = {t.id: t for t in wf.tasks}
        assert tasks["score[1]"].status == TaskStatus.SKIPPED
        assert tasks["score[1]"].skip_reason == SkipReason.DROPPED
        assert tasks["score[1]"].finished_at is not None
        assert tasks["score[2]"].status == TaskStatus.FAILED
        assert tasks["score[2]"].skip_reason is None

    async def test_drop_event_is_warning(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """One WARNING event lists every own drop and its reason."""
        wf, _map = _drop_code_wf(
            tmp_path,
            {"a.txt": "a", "b.txt": "exit 3", "c.txt": "exit 4"},
            on_item_failure="drop",
        )

        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        (event,) = _drop_events(emit)
        assert event.task_id == "score"
        assert event.level == "WARNING"
        assert event.data == {
            "map": {
                "dropped": [
                    {"slot": "1", "item": "b.txt", "reason": "exit code 3"},
                    {
                        "slot": "2",
                        "item": "c.txt",
                        "reason": "CommandFailedError: Shell command exited "
                        "with return code 4",
                    },
                ],
                "upstream_dropped": {},
            }
        }
        assert event.message is not None
        assert "1 (b.txt): Dropped: exit code 3" in event.message

    async def test_config_hash_unchanged_without_new_fields(self) -> None:
        """A map without zip or drop codes hashes as before; setting either
        changes the hash.
        """
        map_task = _map_task(
            over_artifact=FolderArtifact(id="batches", path=Path("batches_in"))
        )
        digests = {"batches": "d"}
        expected = hashlib.sha256(
            json.dumps(
                {
                    "runtime": map_task.runtime.model_dump(mode="json"),
                    "executor": map_task.executor.model_dump(mode="json"),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        assert (await map_task._fingerprint(digests)).config_hash == expected

        map_task.drop_exit_codes = [3]
        with_codes = (await map_task._fingerprint(digests)).config_hash
        assert with_codes != expected
        map_task.zip = [MapOver(input_id="batches", item_id="pair")]
        assert (await map_task._fingerprint(digests)).config_hash not in (
            expected,
            with_codes,
        )

    async def test_a_rerun_retries_only_the_dropped_item(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A partial run leaves no map manifest, so the next run re-enters
        the map: the dropped clone runs again, the others skip on their own
        manifests, and the now-full map writes its manifest.
        """
        del horus_context
        manifest = tmp_path / ".horus" / "score.json"

        def _build() -> HorusWorkflow:
            split = _split_task(tmp_path, ["a.txt", "b.txt", "c.txt"])
            return _wire(
                tmp_path,
                split=split,
                map_task=_dropping_map(command=_FLAKY),
            )

        # _build() rewrites every item to its own name, which fixes b.txt.
        first = _build()
        (tmp_path / "batches" / "b.txt").write_text("bad")
        await first.run(trigger_id="split")
        assert not manifest.exists()

        wf = _build()
        await wf.run(trigger_id="split")

        tasks = {t.id: t for t in wf.tasks}
        assert tasks["score[1]"].status == TaskStatus.COMPLETED
        assert tasks["score[1]"].runs == 1
        for slot in ("0", "2"):
            assert tasks[f"score[{slot}]"].status == TaskStatus.SKIPPED
            assert tasks[f"score[{slot}]"].runs == 0
        score = tasks["score"]
        assert isinstance(score, MapTask)
        assert score._dropped_slots == []
        assert manifest.exists()
        scored = tmp_path / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["0", "1", "2"]

    async def test_cancelling_a_dropping_map_cancels(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A cancel is never absorbed as a dropped item."""
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _dropping_map(command="sleep 5")
        wf = _wire(tmp_path, split=split, map_task=map_task)

        run = asyncio.create_task(wf.run(trigger_id="split"))
        await asyncio.sleep(0.5)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

        assert wf.status.value == "canceled"
        assert map_task.status != TaskStatus.COMPLETED
        assert map_task._dropped_slots == []

    async def test_a_partial_run_removes_a_full_runs_manifest(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A manifest left by an earlier full run cannot survive a partial
        one, or the next run would skip the map with an item missing.
        """
        del horus_context
        manifest = tmp_path / ".horus" / "score.json"
        await _wire(
            tmp_path,
            split=_split_task(tmp_path, ["a.txt", "b.txt"]),
            map_task=_dropping_map(command=_FLAKY),
        ).run(trigger_id="split")
        assert manifest.exists()

        wf = _wire(
            tmp_path,
            split=_split_task(tmp_path, ["a.txt", "b.txt"]),
            map_task=_dropping_map(command=_FLAKY),
        )
        (tmp_path / "batches" / "b.txt").write_text("bad")
        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert not manifest.exists()

    async def test_rerunning_the_same_workflow_resets_the_drops(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Drops belong to one execution: once the item is fixed, a second
        run of the same workflow object records none and writes the
        manifest.
        """
        del horus_context
        manifest = tmp_path / ".horus" / "score.json"
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _dropping_map(command=_FLAKY)
        wf = _wire(tmp_path, split=split, map_task=map_task)
        (tmp_path / "batches" / "b.txt").write_text("bad")
        await wf.run(trigger_id="split")
        assert map_task._dropped_slots == ["1"]

        (tmp_path / "batches" / "b.txt").write_text("b.txt")
        await wf.run(trigger_id="split")

        assert map_task.status == TaskStatus.COMPLETED
        assert map_task._dropped_slots == []
        assert manifest.exists()

    async def test_continue_and_drop_complete(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Under the continue policy a dropped item still completes the
        map and the run.
        """
        del horus_context
        split = _split_task(tmp_path, ["a.txt", "b.txt"])
        map_task = _dropping_map(command=_FLAKY)
        wf = _wire(tmp_path, split=split, map_task=map_task)
        wf.failure_policy = "continue"
        (tmp_path / "batches" / "b.txt").write_text("bad")

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert map_task.status == TaskStatus.COMPLETED
        statuses = {t.id: t.status for t in wf.tasks}
        assert statuses["score[1]"] == TaskStatus.FAILED


@pytest.mark.unit
class TestSlotNames:
    """A map over another map's output keeps the upstream slot names."""

    async def test_a_dropped_upstream_slot_leaves_a_gap(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """report[2] still processes score[2] after score[1] is dropped."""
        del horus_context
        wf = _chain(tmp_path, ["a.txt", "b.txt", "c.txt"])
        (tmp_path / "batches" / "b.txt").write_text("bad")

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        reports = tmp_path / "reports_out"
        assert sorted(p.name for p in reports.iterdir()) == ["0", "2"]
        assert (reports / "2" / "result.txt").read_text() == "c.txt"

    async def test_without_a_gap_the_slot_names_are_unchanged(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Same names as index slots, so existing caches stay valid."""
        del horus_context
        wf = _chain(tmp_path, ["a.txt", "b.txt", "c.txt"])

        await wf.run(trigger_id="split")

        assert sorted(
            t.id for t in wf.tasks if t.id.startswith("report[")
        ) == ["report[0]", "report[1]", "report[2]"]

    async def test_a_newly_dropped_item_removes_its_stale_downstream_slot(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """An earlier result for an item that now fails cannot survive."""
        del horus_context
        await _chain(tmp_path, ["a.txt", "b.txt", "c.txt"]).run(
            trigger_id="split"
        )
        reports = tmp_path / "reports_out"
        assert sorted(p.name for p in reports.iterdir()) == ["0", "1", "2"]

        wf = _chain(tmp_path, ["a.txt", "b.txt", "c.txt"])
        (tmp_path / "batches" / "b.txt").write_text("bad")
        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert sorted(p.name for p in reports.iterdir()) == ["0", "2"]

    async def test_a_first_map_over_numeric_names_uses_index_slots(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Numeric-looking entry names do not make a map inherit them."""
        del horus_context
        split = _split_task(tmp_path, ["102202209", "102202300"])
        wf = _wire(tmp_path, split=split, map_task=_dropping_map())

        await wf.run(trigger_id="split")

        scored = tmp_path / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["0", "1"]


# Set while the ready callbacks run, like terraform's provisioning context.
_READY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_READY", default=False
)


class _Node(LocalTarget):
    """
    A releasable machine at its own location (inputs are copied onto it),
    lost just before the clone ``lose_before`` starts, or while the clone
    ``lose_during`` runs (which then raises ``TargetLostError``): its
    directory is wiped, its generation bumped and its ready callbacks run.
    Like terraform, a leaf task's attempt is pinned to the generation it
    first used, keyed to ``(task.id, _target_loss_retries)``; a map is not.
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "test_map_node"
    releasable: ClassVar[bool] = True
    lose_before: str | None = None
    lose_during: str | None = None

    _node: dict[str, Any] = PrivateAttr(
        default_factory=lambda: {
            "gen": 1,
            "live": True,
            "callbacks": [],
            "errors": [],
            "lost_for": set(),
            "restaging": False,
            "commands": [],
        }
    )
    _pin: tuple[str, int, int] | None = PrivateAttr(default=None)

    @property
    def location_id(self) -> str:
        """Not the orchestrator's filesystem."""
        return "fake://node"

    @property
    def hold_timeout_s(self) -> float:
        """Seconds the machine holds."""
        return 30.0

    @property
    def is_live(self) -> bool:
        """Whether the machine is up; any use of it while down provisions."""
        return bool(self._node["live"])

    @property
    def generation(self) -> int:
        """Provision count."""
        return int(self._node["gen"])

    def on_ready(self, callback: Any) -> None:
        """Keep *callback* for the next generation."""
        self._node["callbacks"].append(callback)

    async def lose(self) -> None:
        """Replace the machine with an empty one."""
        directory = Path(self.resolved_working_directory)
        shutil.rmtree(directory)
        directory.mkdir()
        self._node["gen"] += 1
        self._node["restaging"] = True
        token = _READY.set(True)
        for callback in self._node["callbacks"]:
            try:
                await callback()
            except Exception as exc:  # logged, not raised, like terraform
                self._node["errors"].append(exc)
        _READY.reset(token)
        self._node["restaging"] = False

    def _check_pin(self) -> None:
        """Raise when the bound attempt's generation was replaced."""
        task = self._task
        if task is None or task.dispatches_children or self._node["restaging"]:
            return
        if task.status in TERMINAL_STATUSES:
            self._pin = None
            return
        key = (task.id, task._target_loss_retries)
        if self._pin is None or self._pin[:2] != key:
            self._pin = (*key, self.generation)
        elif self._pin[2] != self.generation:
            raise TargetLostError("The machine was replaced.")

    async def _up(self) -> None:
        """Provision a new, empty generation if the machine is down."""
        if not self._node["live"]:
            self._node["live"] = True
            await self.lose()

    async def get_file(self, remote_path: str) -> bytes:
        """Provision first, like a terraform target."""
        await self._up()
        self._check_pin()
        return await super().get_file(remote_path)

    async def _lose_for(self, task_id: str | None) -> bool:
        """Lose the machine once, if the bound task is *task_id*."""
        task = self._task
        if task is None or task.id != task_id:
            return False
        if task.id in self._node["lost_for"]:
            return False
        self._node["lost_for"].add(task.id)
        await self.lose()
        return True

    async def mkdir(self, path: str) -> None:
        """Lose the machine before the chosen clone first touches it."""
        await self._up()
        await self._lose_for(self.lose_before)
        self._check_pin()
        await super().mkdir(path)

    async def run_command(self, cmd: str, **kwargs: Any) -> ChannelProcess:
        """Lose the machine under the chosen clone."""
        await self._up()
        if await self._lose_for(self.lose_during):
            raise TargetLostError("node lost")
        self._check_pin()
        task = self._task
        self._node["commands"].append((task and task.id, _READY.get()))
        return await super().run_command(cmd, **kwargs)

    async def put_file(self, content: bytes | Path, remote_path: str) -> None:
        """Lose the machine under the chosen task's write."""
        await self._up()
        if await self._lose_for(self.lose_during):
            raise TargetLostError("node lost")
        self._check_pin()
        await super().put_file(content, remote_path)


class _S3Evacuator(BaseEvacuator):
    """Recovers every output from ``stored``, without the machine."""

    add_to_registry: ClassVar[bool] = False
    kind: str = "test_map_s3"
    stored: ClassVar[Path | None] = None

    async def evacuate(
        self, task: Any, output: BaseArtifact, source: Any
    ) -> EdgeSource:
        raise NotImplementedError

    async def recover(
        self, task: Any, output: BaseArtifact
    ) -> EdgeSource | None:
        if self.stored is None:
            return None
        return EdgeSource(
            LocalTarget(),
            output.model_copy(update={"path": self.stored}),
            task.id,
        )


def _task_messages(emit: Any, task_id: str) -> list[str]:
    """Messages of the task events *emit* was called with for *task_id*."""
    return [
        c.args[0].message or ""
        for c in emit.call_args_list
        if isinstance(c.args[0], HorusTaskEvent)
        and c.args[0].task_id == task_id
    ]


def _on_node(
    tmp_path: Path,
    names: list[str],
    *,
    produced_on_node: bool,
    lose_before: str | None = "score[1]",
    lose_during: str | None = None,
) -> tuple[HorusWorkflow, _Node]:
    """
    ``split -> score`` with the map on a node lost before its second clone
    (by default). The split runs on the same node too when
    *produced_on_node*.
    """
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    node = _Node(
        working_directory=node_dir.as_posix(),
        lose_before=lose_before,
        lose_during=lose_during,
    )
    split = _split_task(node_dir if produced_on_node else tmp_path, names)
    if produced_on_node:
        split.target = node
    map_task = _map_task(
        over_artifact=FolderArtifact(id="batches", path=Path("batches_in")),
        output_path=(node_dir / "scored_out").as_posix(),
        max_concurrency=1,
    )
    map_task.target = node
    return _wire(tmp_path, split=split, map_task=map_task), node


@pytest.mark.unit
class TestRestage:
    """The map's machine is replaced while its clones run."""

    async def test_pending_clones_find_their_inputs(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The inputs come back from the orchestrator, slot dirs too, and the
        clone that finished on the lost machine runs again, with an event.
        """
        wf, node = _on_node(
            tmp_path, ["a.txt", "b.txt", "c.txt"], produced_on_node=False
        )

        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert node.generation == 2
        assert node._node["errors"] == []
        assert (
            "Output of score[0] lost with its machine; running it again."
            in _task_messages(emit, "score[0]")
        )
        # Started by the restage, not after the first wave.
        assert list(cast(MapTask, wf.tasks[1])._early_reruns) == [0]
        scored = tmp_path / "node" / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["0", "1", "2"]
        for slot, name in enumerate(("a.txt", "b.txt", "c.txt")):
            assert (scored / str(slot) / "result.txt").read_text() == name

    async def test_early_reruns_do_not_inherit_the_ready_context(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A re-run started from a ready callback runs in the map's context,
        so a target's provisioning state set there does not leak into it.
        """
        del horus_context
        wf, node = _on_node(
            tmp_path, ["a.txt", "b.txt", "c.txt"], produced_on_node=False
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert list(cast(MapTask, wf.tasks[1])._early_reruns) == [0]
        commands = node._node["commands"]
        assert ("score[0]", False) in commands
        assert not any(ready for _, ready in commands)

    async def test_the_map_completes_under_the_pin(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The clone that first read the lost generation fails its attempt on
        the pin and retries; the map, not pinned, writes its manifest on
        the new generation and completes.
        """
        wf, node = _on_node(
            tmp_path, ["a.txt", "b.txt", "c.txt"], produced_on_node=False
        )

        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert node.generation == 2
        assert any(
            "Node lost; retrying score[1]" in m
            for m in _task_messages(emit, "score[1]")
        )
        assert wf.tasks[1]._target_loss_retries == 0
        assert (tmp_path / "node" / ".horus" / "score.json").exists()

    async def test_a_clone_lost_mid_run_is_retried(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The clone running when the machine goes runs again on the new one;
        the folder has every slot.
        """
        del horus_context
        wf, node = _on_node(
            tmp_path,
            ["a.txt", "b.txt", "c.txt"],
            produced_on_node=False,
            lose_before=None,
            lose_during="score[1]",
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert node.generation == 2
        scored = tmp_path / "node" / "scored_out"
        for slot, name in enumerate(("a.txt", "b.txt", "c.txt")):
            assert (scored / str(slot) / "result.txt").read_text() == name

    async def test_a_machine_down_after_the_fan_out_reruns_every_slot(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The machine dies after the last clone, before any re-provision:
        every slot is lost and runs again on the next generation.
        """
        del horus_context
        wf, node = _on_node(
            tmp_path,
            ["a.txt", "b.txt", "c.txt"],
            produced_on_node=False,
            lose_before=None,
        )
        rerun_lost = MapTask._rerun_lost

        async def die_first(self: MapTask, *args: Any) -> None:
            node._node["live"] = False
            await rerun_lost(self, *args)

        monkeypatch.setattr(MapTask, "_rerun_lost", die_first)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert node.generation == 2
        scored = tmp_path / "node" / "scored_out"
        for slot, name in enumerate(("a.txt", "b.txt", "c.txt")):
            assert (scored / str(slot) / "result.txt").read_text() == name

    async def test_a_loss_after_the_fan_out_retries_the_map(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The machine goes while the map writes its manifest: the map runs
        again on the next generation and its folder has every slot.
        """
        del horus_context
        wf, node = _on_node(
            tmp_path,
            ["a.txt", "b.txt", "c.txt"],
            produced_on_node=False,
            lose_before=None,
            lose_during="score",
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert node.generation == 2
        scored = tmp_path / "node" / "scored_out"
        for slot, name in enumerate(("a.txt", "b.txt", "c.txt")):
            assert (scored / str(slot) / "result.txt").read_text() == name

    async def test_pinned_inputs_are_not_restaged_silently(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A map whose inputs a loop pinned in place says it cannot restage."""
        wf, node = _on_node(
            tmp_path, ["a.txt"], produced_on_node=False, lose_before=None
        )
        map_task = cast(MapTask, wf.tasks[1])
        map_task.inputs[0].pinned = True
        map_task.status = TaskStatus.RUNNING
        node._node["gen"] = 2
        horus_context.workflow = wf

        with (
            patch.object(
                horus_context.bus, "emit", wraps=horus_context.bus.emit
            ) as emit,
            pytest.raises(RuntimeError, match="pinned"),
        ):
            await map_task._restage()

        assert any(
            "could not restage" in m for m in _task_messages(emit, "score")
        )

    async def test_a_second_loss_without_budget_fails_the_map(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A clone already retried once, whose slot is lost again, fails the
        map instead of leaving its slot missing.
        """
        del horus_context
        wf, node = _on_node(
            tmp_path,
            ["a.txt", "b.txt", "c.txt"],
            produced_on_node=False,
            lose_during="score[0]",
        )

        with pytest.raises(ExceptionGroup) as excinfo:
            await wf.run(trigger_id="split")
        assert excinfo.group_contains(TargetLostError)

        assert node.generation == 3
        assert wf.status.value == "failed"

    async def test_a_second_loss_without_budget_drops_the_item(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """With ``drop``, that clone's slot is dropped and reported."""
        wf, node = _on_node(
            tmp_path,
            ["a.txt", "b.txt", "c.txt"],
            produced_on_node=False,
            lose_during="score[0]",
        )
        cast(MapTask, wf.tasks[1]).on_item_failure = "drop"

        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        assert node.generation == 3
        assert wf.status.value == "completed"
        scored = tmp_path / "node" / "scored_out"
        assert sorted(p.name for p in scored.iterdir()) == ["1", "2"]
        [message] = _drop_messages(emit)
        assert "lost with its machine" in message
        clone = next(t for t in wf.tasks if t.id == "score[0]")
        assert clone.status == TaskStatus.FAILED
        assert _task_messages(emit, "score[0]")[-1] == (
            "The output of score[0] was lost with its machine."
        )

    async def test_producer_on_the_lost_node_is_recovered(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Run e8454042: the map's input was produced on the same node, so it
        comes back from the evacuator's copy.
        """
        del horus_context
        copy = tmp_path / "s3" / "batches"
        copy.mkdir(parents=True)
        for name in ("a.txt", "b.txt", "c.txt"):
            (copy / name).write_text(name)
        monkeypatch.setattr(_S3Evacuator, "stored", copy)
        monkeypatch.setattr(lease_module, "_pick_evacuator", _S3Evacuator)
        wf, node = _on_node(
            tmp_path, ["a.txt", "b.txt", "c.txt"], produced_on_node=True
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert node._node["errors"] == []
        scored = tmp_path / "node" / "scored_out"
        for slot, name in ((1, "b.txt"), (2, "c.txt")):
            assert (scored / str(slot) / "result.txt").read_text() == name

    async def test_chained_clone_recovers_lost_source_item(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A's node is lost after A[1] finished and before B[1] read it: B[1]
        reads A[1]'s own copy from the evacuator, per clone.
        """
        del horus_context
        copy = tmp_path / "s3" / "a1"
        copy.mkdir(parents=True)
        (copy / "result.txt").write_text("from s3")
        monkeypatch.setattr(_S3Evacuator, "stored", copy)
        monkeypatch.setattr(lease_module, "_pick_evacuator", _S3Evacuator)
        node_dir = tmp_path / "node"
        node_dir.mkdir()
        node = _Node(working_directory=node_dir.as_posix())
        a = _stage_map(tmp_path, "a", target=node)
        a.outputs[0].path = node_dir / "a_out"
        b = _stage_map(tmp_path, "b", chain=True)
        wf = _stages(tmp_path, ["x", "y"], [a, b])
        wait = _ItemStream.wait
        lost: list[str] = []

        async def lose_first(
            self: _ItemStream, slot: str, *, owner: str, source: str
        ) -> Any:
            item = await wait(self, slot, owner=owner, source=source)
            if owner == "b" and slot == "1" and not lost:
                lost.append(slot)
                while a.status != TaskStatus.COMPLETED:
                    await asyncio.sleep(0.01)
                await node.lose()
            return item

        monkeypatch.setattr(_ItemStream, "wait", lose_first)

        await asyncio.wait_for(wf.run(trigger_id="split"), timeout=10)

        assert wf.status.value == "completed"
        assert node.generation == 2
        assert _result(tmp_path, "b", "1") == "from s3>b_out_1\n"
        # A's folder is gone, so B records no manifest: a miss next time.
        assert not (tmp_path / ".horus" / "b.json").exists()

    async def test_nothing_to_restage_from_fails_loudly(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No copy of the lost input: an event says why the clones fail."""
        monkeypatch.setattr(_S3Evacuator, "stored", None)
        monkeypatch.setattr(lease_module, "_pick_evacuator", _S3Evacuator)
        wf, node = _on_node(
            tmp_path, ["a.txt", "b.txt", "c.txt"], produced_on_node=True
        )

        with (
            patch.object(
                horus_context.bus, "emit", wraps=horus_context.bus.emit
            ) as emit,
            pytest.raises(ExceptionGroup) as excinfo,
        ):
            await wf.run(trigger_id="split")
        assert excinfo.group_contains(ArtifactDoesNotExistError)

        assert wf.status.value == "failed"
        assert [type(e) for e in node._node["errors"]] == [ArtifactLostError]
        assert any(
            isinstance(c.args[0], HorusTaskEvent)
            and "could not restage" in (c.args[0].message or "")
            for c in emit.call_args_list
        )


def _stage_body(tmp_path: Path) -> str:
    """
    A map body: write the text of its item and its zip ``mate`` (each a
    file, or a slot holding ``result.txt``) plus ``>map_slot`` into
    ``result.txt`` of its slot.
    Logs start and end to ``log``; sleeps ``delay/<key>`` seconds and exits
    with ``exit/<key>`` first, where the key is ``<map>_out_<slot>``.
    """
    return (
        "k=$(basename $(dirname $out))_$(basename $out); "
        f'echo "$k start $(date +%s.%N)" >> {tmp_path}/log; '
        f"[ -f {tmp_path}/delay/$k ] && sleep $(cat {tmp_path}/delay/$k); "
        f"[ -f {tmp_path}/exit/$k ] && exit $(cat {tmp_path}/exit/$k); "
        "{ for i in $item $mate; do if [ -d $i ]; then cat $i/result.txt; "
        'else cat $i; fi; done; echo ">$k"; } > $out/result.txt; '
        f'echo "$k end $(date +%s.%N)" >> {tmp_path}/log'
    )


def _set(tmp_path: Path, what: str, key: str, value: object) -> None:
    """Make clone *key* (``<map>_out_<slot>``) sleep or exit with *value*."""
    (tmp_path / what).mkdir(exist_ok=True)
    (tmp_path / what / key).write_text(str(value))


def _when(tmp_path: Path, key: str, what: str) -> float:
    """When clone *key* last logged *what* (``start`` or ``end``)."""
    return max(
        float(line.split()[2])
        for line in (tmp_path / "log").read_text().splitlines()
        if line.split()[:2] == [key, what]
    )


def _result(tmp_path: Path, map_id: str, slot: str) -> str:
    """What clone *slot* of *map_id* wrote."""
    return (tmp_path / f"{map_id}_out" / slot / "result.txt").read_text()


def _stage_map(
    tmp_path: Path, task_id: str, *, chain: bool = False, **kwargs: Any
) -> MapTask:
    """A ``_stage_body`` map over its ``in`` input, into ``<id>_out``."""
    return MapTask(
        id=task_id,
        name=task_id,
        over=MapOver(input_id="in", item_id="item"),
        runtime=CommandRuntime(command=_stage_body(tmp_path)),
        executor=ShellExecutor(),
        inputs=[FolderArtifact(id="in", path=Path(f"{task_id}_in"))],
        outputs=[FolderArtifact(id="out", path=tmp_path / f"{task_id}_out")],
        chain=chain,
        **{"target": LocalTarget(), "max_concurrency": 0, **kwargs},
    )


def _stages(
    tmp_path: Path,
    names: list[str],
    maps: list[MapTask],
    *,
    extra_tasks: list[BaseTask] | None = None,
    extra_edges: list[WorkflowEdge] | None = None,
    **kwargs: Any,
) -> HorusWorkflow:
    """``split -> maps[0] -> maps[1] -> ...``, each over the one before."""
    split = _split_task(tmp_path, names)
    sources = [("split", "batches"), *((m.id, "out") for m in maps[:-1])]
    return HorusWorkflow(
        name="wf",
        tasks=[split, *maps, *(extra_tasks or [])],
        edges=[
            *(
                _edge(source, output, m.id, "in")
                for (source, output), m in zip(sources, maps, strict=True)
            ),
            *(extra_edges or []),
        ],
        orchestrator_target=LocalTarget(working_directory=tmp_path.as_posix()),
        **kwargs,
    )


def _clone_states(wf: BaseWorkflow, map_id: str) -> dict[str, Any]:
    """Status, and skip reason when skipped, of each clone of *map_id*."""
    return {
        t.id: (t.status, t.skip_reason) if t.skip_reason else t.status
        for t in wf.tasks
        if t.id.startswith(f"{map_id}[")
    }


@pytest.mark.unit
class TestChain:
    """``chain: true``: B[X] starts as soon as A[X] is ready."""

    async def test_b0_starts_before_a_last_clone_ends(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A slow last item does not hold back the others' next stage."""
        del horus_context
        _set(tmp_path, "delay", "a_out_2", 0.5)
        wf = _stages(
            tmp_path,
            ["x", "y", "z"],
            [_stage_map(tmp_path, "a"), _stage_map(tmp_path, "b", chain=True)],
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert _when(tmp_path, "b_out_0", "start") < _when(
            tmp_path, "a_out_2", "end"
        )
        assert _result(tmp_path, "b", "2") == "z>a_out_2\n>b_out_2\n"

    async def test_three_map_chain_flows_per_item(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """C[0] finishes while A[2] still runs; every item reaches C."""
        del horus_context
        _set(tmp_path, "delay", "a_out_2", 0.5)
        wf = _stages(
            tmp_path,
            ["x", "y", "z"],
            [
                _stage_map(tmp_path, "a"),
                _stage_map(tmp_path, "b", chain=True),
                _stage_map(tmp_path, "c", chain=True),
            ],
        )

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert _when(tmp_path, "c_out_0", "end") < _when(
            tmp_path, "a_out_2", "end"
        )
        for slot, name in enumerate("xyz"):
            assert _result(tmp_path, "c", str(slot)) == (
                f"{name}>a_out_{slot}\n>b_out_{slot}\n>c_out_{slot}\n"
            )

    async def test_upstream_drop_skips_down_the_chain(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A drop in A skips that item in B and C, which leave a gap and say
        so once, naming A as the origin; the run completes.
        """
        _set(tmp_path, "exit", "a_out_1", 3)
        wf = _stages(
            tmp_path,
            ["x", "y", "z"],
            [
                _stage_map(tmp_path, "a", drop_exit_codes=[3]),
                _stage_map(tmp_path, "b", chain=True),
                _stage_map(tmp_path, "c", chain=True),
            ],
        )

        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert _clone_states(wf, "a")["a[1]"] == (
            TaskStatus.SKIPPED,
            SkipReason.DROPPED,
        )
        for map_id in "bc":
            assert _clone_states(wf, map_id) == {
                f"{map_id}[0]": TaskStatus.COMPLETED,
                f"{map_id}[1]": (
                    TaskStatus.SKIPPED,
                    SkipReason.UPSTREAM_DROPPED,
                ),
                f"{map_id}[2]": TaskStatus.COMPLETED,
            }
            out = tmp_path / f"{map_id}_out"
            assert sorted(p.name for p in out.iterdir()) == ["0", "2"]
        events = {
            e.task_id: e for e in _drop_events(emit) if e.level == "WARNING"
        }
        assert events["b"].level == "WARNING"
        assert events["b"].message == "1 item(s) dropped upstream."
        assert events["b"].data["map"] == {
            "dropped": [],
            "upstream_dropped": {"1": "a"},
        }
        assert events["c"].data["map"]["upstream_dropped"] == {"1": "a"}
        assert [d["slot"] for d in events["a"].data["map"]["dropped"]] == ["1"]
        assert not (tmp_path / "log").read_text().count("b_out_1")

    async def test_chained_event_names_the_sources(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """B says once, at the start, which map it follows per item."""
        wf = _stages(
            tmp_path,
            ["x", "y"],
            [_stage_map(tmp_path, "a"), _stage_map(tmp_path, "b", chain=True)],
        )

        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        (event,) = [
            e for e in _drop_events(emit) if e.task_id == "b" and e.data
        ]
        assert event.message == "Map b starts each item when a finishes it."
        assert event.data == {
            "map": {"chain": ["a"], "slots": {"0": "x", "1": "y"}}
        }

    @pytest.mark.parametrize("policy", ["fail_fast", "continue"])
    @pytest.mark.parametrize(
        ("end", "a_fields"),
        [
            ("exit code", {}),
            ("drop code", {"drop_exit_codes": [3]}),
            ("error under drop", {"on_item_failure": "drop"}),
            ("placement failure", {}),
            ("placement failure under drop", {"on_item_failure": "drop"}),
            ("node lost, retries spent", {}),
            ("executor error", {}),
        ],
    )
    async def test_every_non_success_end_settles_b(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        bound_failures: dict[str, BaseException],
        monkeypatch: pytest.MonkeyPatch,
        end: str,
        a_fields: dict[str, Any],
        policy: str,
    ) -> None:
        """
        However A[1] ends short of success, B[1] settles: dropped when A
        drops it, else B is cancelled or blocked with A. Nothing hangs.
        """
        del horus_context
        _set(tmp_path, "delay", "a_out_2", 0.3)
        if end in ("exit code", "error under drop"):
            _set(tmp_path, "exit", "a_out_1", 1)
        elif end == "drop code":
            _set(tmp_path, "exit", "a_out_1", 3)
        elif end.startswith("placement failure"):
            bound_failures["a[1]"] = RuntimeError("no room")
        elif end == "node lost, retries spent":
            bound_failures["a[1]"] = TargetLostError("node lost")
        else:
            execute = ShellExecutor._execute

            async def broken(self: ShellExecutor, task: BaseTask) -> None:
                if task.id == "a[1]":
                    raise TaskExecutionError("executor broke")
                await execute(self, task)

            monkeypatch.setattr(ShellExecutor, "_execute", broken)
        a = _stage_map(tmp_path, "a", **a_fields)
        b = _stage_map(tmp_path, "b", chain=True)
        wf = _stages(tmp_path, ["x", "y", "z"], [a, b], failure_policy=policy)
        run = asyncio.wait_for(wf.run(trigger_id="split"), timeout=20)

        if "drop" in end:
            await run
            assert wf.status.value == "completed"
            assert _clone_states(wf, "b") == {
                "b[0]": TaskStatus.COMPLETED,
                "b[1]": (TaskStatus.SKIPPED, SkipReason.UPSTREAM_DROPPED),
                "b[2]": TaskStatus.COMPLETED,
            }
            return
        with pytest.raises(Exception) as excinfo:
            await run
        assert not isinstance(excinfo.value, TimeoutError), excinfo.value
        assert a.status == TaskStatus.FAILED
        clones = [t for t in wf.tasks if t.id.startswith("b[")]
        assert not any(
            t.status
            in (TaskStatus.PENDING, TaskStatus.CONFIGURING, TaskStatus.RUNNING)
            for t in clones
        )
        if policy == "fail_fast":
            assert b.status == TaskStatus.CANCELED
        else:
            assert (b.status, b.skip_reason) == (
                TaskStatus.SKIPPED,
                SkipReason.UPSTREAM_FAILED,
            )
            assert all(t.status in TERMINAL_STATUSES for t in clones), (
                type(excinfo.value),
                [(t.id, t.status) for t in clones],
            )
            assert clones[1].skip_reason == SkipReason.UPSTREAM_FAILED

    async def test_run_cancel_unwinds_waiting_clones(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Cancelling the run while B[2] waits on A[2] ends both at once."""
        del horus_context
        _set(tmp_path, "delay", "a_out_2", 5)
        wf = _stages(
            tmp_path,
            ["x", "y", "z"],
            [_stage_map(tmp_path, "a"), _stage_map(tmp_path, "b", chain=True)],
        )
        run = asyncio.create_task(wf.run(trigger_id="split"))
        log = tmp_path / "log"
        while not (log.exists() and "b_out_1 end" in log.read_text()):
            await asyncio.sleep(0.02)

        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, timeout=3)

        a, b = wf.tasks[1], wf.tasks[2]
        assert a.status == TaskStatus.CANCELED
        assert b.status == TaskStatus.CANCELED
        assert "b_out_2" not in log.read_text()

    async def test_fail_fast_cancels_waiting_clones(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A's failure cancels B at once: B[2] never starts."""
        del horus_context
        _set(tmp_path, "delay", "a_out_1", 0.2)
        _set(tmp_path, "exit", "a_out_1", 1)
        _set(tmp_path, "delay", "a_out_2", 5)
        a = _stage_map(tmp_path, "a")
        b = _stage_map(tmp_path, "b", chain=True)
        wf = _stages(tmp_path, ["x", "y", "z"], [a, b])

        with pytest.raises(ExceptionGroup):
            await asyncio.wait_for(wf.run(trigger_id="split"), timeout=3)

        assert b.status == TaskStatus.CANCELED
        assert _clone_states(wf, "b")["b[0]"] == TaskStatus.COMPLETED
        assert "b_out_2" not in (tmp_path / "log").read_text()

    async def test_continue_stops_new_clones_and_blocks(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Under continue, A finishes its other items, B starts none after
        A[1] fails and is blocked; B[0] keeps its result.
        """
        del horus_context
        _set(tmp_path, "delay", "a_out_1", 0.2)
        _set(tmp_path, "exit", "a_out_1", 1)
        _set(tmp_path, "delay", "a_out_2", 0.5)
        a = _stage_map(tmp_path, "a")
        b = _stage_map(tmp_path, "b", chain=True)
        wf = _stages(
            tmp_path, ["x", "y", "z"], [a, b], failure_policy="continue"
        )

        with pytest.raises(WorkflowExecutionError):
            await asyncio.wait_for(wf.run(trigger_id="split"), timeout=5)

        assert _clone_states(wf, "a")["a[2]"] == TaskStatus.COMPLETED
        blocked = (TaskStatus.SKIPPED, SkipReason.UPSTREAM_FAILED)
        assert (b.status, b.skip_reason) == blocked
        assert _clone_states(wf, "b") == {
            "b[0]": TaskStatus.COMPLETED,
            "b[1]": blocked,
            "b[2]": blocked,
        }
        assert "b_out_2" not in (tmp_path / "log").read_text()

    async def test_source_loss_retry_keeps_stream_open(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A loss of A's own machine after its fan-out runs A again; B keeps
        waiting on the same stream instead of failing, and completes.
        """
        del horus_context
        settle = MapTask._settle
        lost: list[str] = []

        async def lose_once(self: MapTask, *args: Any) -> None:
            if self.id == "a" and not lost:
                lost.append(self.id)
                raise TargetLostError("node lost")
            await settle(self, *args)

        monkeypatch.setattr(MapTask, "_settle", lose_once)
        a = _stage_map(tmp_path, "a")
        b = _stage_map(tmp_path, "b", chain=True)
        wf = _stages(tmp_path, ["x", "y"], [a, b])

        await asyncio.wait_for(wf.run(trigger_id="split"), timeout=10)

        assert wf.status.value == "completed"
        assert lost == ["a"]
        assert a._target_loss_retries == 1
        assert _clone_states(wf, "b") == {
            "b[0]": TaskStatus.COMPLETED,
            "b[1]": TaskStatus.COMPLETED,
        }

    async def test_chained_clone_drops_whole_streamed_folders(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A chained clone gets its item only: not the streamed folder, which
        B never stages, nor an item side artifact.
        """
        del horus_context
        a = _stage_map(tmp_path, "a")
        b = _stage_map(tmp_path, "b", chain=True)
        wf = _stages(tmp_path, ["x", "y"], [a, b])

        await wf.run(trigger_id="split")

        clone = next(t for t in wf.tasks if t.id == "b[0]")
        assert [i.id for i in clone.inputs] == ["item"]
        assert not any(s.id.endswith("_item") for s in clone.side_artifacts)
        # A's clone still records its item.
        a0 = next(t for t in wf.tasks if t.id == "a[0]")
        assert "a[0]_item" in [s.id for s in a0.side_artifacts]

    async def test_chained_clone_sources_from_a_clone(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """B[X] reads A[X]'s slot from A[X] itself, digested at the source."""
        del horus_context
        seen: dict[str, EdgeSource] = {}
        real = execute_task

        async def spy(wf: BaseWorkflow, task: BaseTask, **kwargs: Any) -> None:
            source = kwargs["source_map"].get((task.id, "item"))
            if source is not None:
                seen[task.id] = source
            await real(wf, task, **kwargs)

        monkeypatch.setattr("horus_builtin.workflow.map.execute_task", spy)
        a = _stage_map(tmp_path, "a")
        b = _stage_map(tmp_path, "b", chain=True)
        wf = _stages(tmp_path, ["x", "y"], [a, b])

        await wf.run(trigger_id="split")

        source = seen["b[1]"]
        assert source.task_id == "a[1]"
        assert source.artifact is not None
        assert Path(source.artifact.path) == tmp_path / "a_out" / "1"
        assert source.digest is None
        assert source.report is False
        assert seen["a[1]"].task_id is None

    async def test_same_target_item_is_not_copied(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        On one shared filesystem (e.g. a Slurm cluster), B[X] reads A[X]'s
        slot in place: no transfer runs and nothing is staged.
        """
        del horus_context
        cluster = _Cluster(working_directory=tmp_path.as_posix())

        async def no_transfer(*_: Any, **__: Any) -> None:
            raise AssertionError("a transfer ran")

        monkeypatch.setattr(
            "horus_runtime.core.transfer.strategy.TransferMiddleware."
            "call_with_middleware",
            no_transfer,
        )
        a = _stage_map(tmp_path, "a", target=cluster)
        b = _stage_map(tmp_path, "b", chain=True, target=cluster)
        wf = _stages(tmp_path, ["x", "y"], [a, b])
        wf.tasks[0].target = cluster

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        clone = next(t for t in wf.tasks if t.id == "b[1]")
        assert Path(clone.inputs[0].path) == tmp_path / "a_out" / "1"
        assert _result(tmp_path, "b", "1") == "y>a_out_1\n>b_out_1\n"
        assert not (tmp_path / "b_in").exists()

    async def test_all_items_dropped_fails_the_map(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Dropped upstream or here, every item gone fails B."""
        del horus_context
        _set(tmp_path, "exit", "a_out_1", 3)
        _set(tmp_path, "exit", "b_out_0", 4)
        _set(tmp_path, "exit", "b_out_2", 4)
        a = _stage_map(tmp_path, "a", drop_exit_codes=[3])
        b = _stage_map(tmp_path, "b", chain=True, drop_exit_codes=[4])
        wf = _stages(tmp_path, ["x", "y", "z"], [a, b])

        with pytest.raises(TaskExecutionError):
            await asyncio.wait_for(wf.run(trigger_id="split"), timeout=10)

        assert a.status == TaskStatus.COMPLETED
        assert b.status == TaskStatus.FAILED
        assert b.error == "TaskExecutionError: All 3 items were dropped in b."

    async def test_all_dropped_source_blocks_b(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A drops everything: A fails, and B is blocked, not failed."""
        del horus_context
        for slot in range(3):
            _set(tmp_path, "exit", f"a_out_{slot}", 3)
        a = _stage_map(tmp_path, "a", drop_exit_codes=[3])
        b = _stage_map(tmp_path, "b", chain=True)
        wf = _stages(
            tmp_path, ["x", "y", "z"], [a, b], failure_policy="continue"
        )

        with pytest.raises(WorkflowExecutionError):
            await asyncio.wait_for(wf.run(trigger_id="split"), timeout=10)

        assert a.error == "TaskExecutionError: All 3 items were dropped in a."
        assert (b.status, b.skip_reason) == (
            TaskStatus.SKIPPED,
            SkipReason.UPSTREAM_FAILED,
        )

    async def test_gather_after_all_dropped_is_blocked(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """No empty folder reaches a gather after an all-dropped chain."""
        del horus_context
        for slot in range(2):
            _set(tmp_path, "exit", f"b_out_{slot}", 3)
        gather = HorusTask(
            id="gather",
            name="gather",
            runtime=CommandRuntime(command="ls $all > $listing"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            inputs=[FolderArtifact(id="all", path=Path("all_in"))],
            outputs=[FileArtifact(id="listing", path=tmp_path / "ls.txt")],
        )
        wf = _stages(
            tmp_path,
            ["x", "y"],
            [
                _stage_map(tmp_path, "a"),
                _stage_map(tmp_path, "b", chain=True, drop_exit_codes=[3]),
            ],
            extra_tasks=[gather],
            extra_edges=[_edge("b", "out", "gather", "all")],
            failure_policy="continue",
        )

        with pytest.raises(WorkflowExecutionError):
            await asyncio.wait_for(wf.run(trigger_id="split"), timeout=10)

        assert (gather.status, gather.skip_reason) == (
            TaskStatus.SKIPPED,
            SkipReason.UPSTREAM_FAILED,
        )
        assert not (tmp_path / "ls.txt").exists()

    async def test_second_run_skips_everything(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Nothing changed: both maps skip whole, nothing runs."""
        del horus_context

        def chain() -> HorusWorkflow:
            return _stages(
                tmp_path,
                ["x", "y"],
                [
                    _stage_map(tmp_path, "a"),
                    _stage_map(tmp_path, "b", chain=True),
                ],
            )

        await chain().run(trigger_id="split")
        _starts(tmp_path)

        wf = chain()
        await wf.run(trigger_id="split")

        assert _starts(tmp_path) == set()
        for task in wf.tasks[1:]:
            assert (task.status, task.skip_reason) == (
                TaskStatus.SKIPPED,
                SkipReason.COMPLETE,
            )

    async def test_one_item_change_reruns_only_its_chain(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A changed item re-runs that item in A, B and C only."""
        del horus_context

        def chain() -> HorusWorkflow:
            return _stages(
                tmp_path,
                ["x", "y", "z"],
                [
                    _stage_map(tmp_path, "a"),
                    _stage_map(tmp_path, "b", chain=True),
                    _stage_map(tmp_path, "c", chain=True),
                ],
            )

        await chain().run(trigger_id="split")
        _starts(tmp_path)

        wf = chain()
        (tmp_path / "batches" / "y").write_text("Y")
        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert _starts(tmp_path) == {"a_out_1", "b_out_1", "c_out_1"}
        assert _result(tmp_path, "c", "1").startswith("Y>a_out_1")

    async def test_source_skipped_whole_takes_normal_path(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A skips whole, so B starts after it like any consumer: it reads
        A's folder, says nothing about a chain, and still runs per item.
        """

        def chain(command: str) -> HorusWorkflow:
            b = _stage_map(tmp_path, "b", chain=True)
            b.runtime = CommandRuntime(command=command)
            return _stages(
                tmp_path, ["x", "y"], [_stage_map(tmp_path, "a"), b]
            )

        body = _stage_body(tmp_path)
        await chain(body).run(trigger_id="split")
        _starts(tmp_path)

        wf = chain(body + "; true")
        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert wf.tasks[1].skip_reason == SkipReason.COMPLETE
        assert _starts(tmp_path) == {"b_out_0", "b_out_1"}
        assert not [e for e in _drop_events(emit) if "chain" in e.data["map"]]

    async def test_rerun_after_drop_retries_only_that_item(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        The next run re-enters A and B for the item A dropped; their other
        clones skip on their own manifests.
        """
        del horus_context

        def chain() -> HorusWorkflow:
            return _stages(
                tmp_path,
                ["x", "y", "z"],
                [
                    _stage_map(tmp_path, "a", drop_exit_codes=[3]),
                    _stage_map(tmp_path, "b", chain=True),
                ],
            )

        _set(tmp_path, "exit", "a_out_1", 3)
        await chain().run(trigger_id="split")
        _starts(tmp_path)
        (tmp_path / "exit" / "a_out_1").unlink()

        wf = chain()
        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert _starts(tmp_path) == {"a_out_1", "b_out_1"}
        assert sorted(p.name for p in (tmp_path / "b_out").iterdir()) == [
            "0",
            "1",
            "2",
        ]

    async def test_streamed_input_stays_in_map_manifest(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """B's manifest holds the digest of A's final folder."""
        del horus_context
        wf = _stages(
            tmp_path,
            ["x", "y"],
            [_stage_map(tmp_path, "a"), _stage_map(tmp_path, "b", chain=True)],
        )

        await wf.run(trigger_id="split")

        manifest = json.loads((tmp_path / ".horus" / "b.json").read_text())
        digest = await ArtifactStore(LocalTarget()).digest(
            FolderArtifact(id="out", path=tmp_path / "a_out")
        )
        assert manifest["inputs"] == {"in": digest}

    async def test_manifest_independent_of_finish_order(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        Whichever of A's clones finishes first, B records the manifest a
        run that did not stream records.
        """
        del horus_context
        manifests = []
        for slow, chain in (("a_out_0", True), ("a_out_2", True), ("", False)):
            for name in ("a_out", "b_out", ".horus", "delay"):
                shutil.rmtree(tmp_path / name, ignore_errors=True)
            if slow:
                _set(tmp_path, "delay", slow, 0.3)
            wf = _stages(
                tmp_path,
                ["x", "y", "z"],
                [
                    _stage_map(tmp_path, "a"),
                    _stage_map(tmp_path, "b", chain=chain),
                ],
            )
            await wf.run(trigger_id="split")
            manifests.append((tmp_path / ".horus" / "b.json").read_text())

        assert manifests[0] == manifests[1] == manifests[2]

    async def test_streaming_map_never_skips_at_map_level(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        While B streams, its skip checks say no without digesting A's
        partial folder.
        """
        a = _stage_map(tmp_path, "a")
        b = _stage_map(tmp_path, "b", chain=True)
        wf = _stages(tmp_path, ["x", "y"], [a, b])
        horus_context.workflow = wf
        b.stream_sources(wf.cached_source_map(None)[1])
        b._streaming_from = frozenset({"a"})
        digests = AsyncMock(return_value={"in": "d"})

        assert b.streamed_inputs == {"in"}
        assert await b.is_complete(digests) is False
        assert await b.is_complete_before_transfer(digests) is False
        assert await b.manifest_matches(b"{}", digests) is False
        digests.assert_not_awaited()

    async def test_chain_overlaps_with_workflow_max_concurrency_one(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Waiting maps take no workflow slot, so B still overlaps A."""
        del horus_context
        _set(tmp_path, "delay", "a_out_2", 0.5)
        wf = _stages(
            tmp_path,
            ["x", "y", "z"],
            [_stage_map(tmp_path, "a"), _stage_map(tmp_path, "b", chain=True)],
            max_concurrency=1,
        )

        await asyncio.wait_for(wf.run(trigger_id="split"), timeout=10)

        assert wf.status.value == "completed"
        assert _when(tmp_path, "b_out_0", "start") < _when(
            tmp_path, "a_out_2", "end"
        )

    async def test_placement_capacity_respected_in_chain(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        One CPU for every clone of A and B: they run one at a time, and a
        waiting B clone holds none of it.
        """
        del horus_context
        for slot in range(3):
            _set(tmp_path, "delay", f"a_out_{slot}", 0.05)
        cpu = ResourceRequest(cpus=1)
        wf = _stages(
            tmp_path,
            ["x", "y", "z"],
            [
                _stage_map(tmp_path, "a", resources=cpu),
                _stage_map(tmp_path, "b", chain=True, resources=cpu),
            ],
            capacity={LocalTarget().location_id: ResourceCapacity(cpus=1)},
        )

        await asyncio.wait_for(wf.run(trigger_id="split"), timeout=10)

        assert wf.status.value == "completed"
        spans = sorted(
            (_when(tmp_path, key, "start"), _when(tmp_path, key, "end"))
            for key in (f"{m}_out_{i}" for m in "ab" for i in range(3))
        )
        assert all(
            end <= start for (_, end), (start, _) in itertools.pairwise(spans)
        )

    async def test_stream_future_reused_then_reset_per_run(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        B gets the same future for A's stream until A runs; after the run
        A hands out a new, unopened one.
        """
        a = _stage_map(tmp_path, "a")
        b = _stage_map(tmp_path, "b", chain=True)
        wf = _stages(tmp_path, ["x"], [a, b])
        horus_context.workflow = wf
        source_map = wf.cached_source_map(None)[1]

        first = b.stream_sources(source_map)
        assert first == {"a": a.stream_for_consumers().opened}
        assert b.stream_sources(source_map)["a"] is first["a"]

        horus_context.workflow = None
        await wf.run(trigger_id="split")

        assert first["a"].done()
        assert a._stream is None
        horus_context.workflow = wf
        again = b.stream_sources(wf.cached_source_map(None)[1])["a"]
        assert again is not first["a"]
        assert not again.done()


def _zip_map(
    tmp_path: Path, task_id: str, *, chain: bool = False, **kwargs: Any
) -> MapTask:
    """A ``_stage_map`` that also pairs its ``mates`` input as ``mate``."""
    task = _stage_map(tmp_path, task_id, chain=chain, **kwargs)
    task.inputs.append(FolderArtifact(id="mates", path=Path("mates_in")))
    task.zip = [MapOver(input_id="mates", item_id="mate")]
    return task


def _zipped(
    tmp_path: Path,
    names: list[str],
    *,
    chain: bool = False,
    a: dict[str, Any] | None = None,
    c: dict[str, Any] | None = None,
) -> HorusWorkflow:
    """``split -> a -> b`` and ``split -> c``, b pairing c's items."""
    return _stages(
        tmp_path,
        names,
        [
            _stage_map(tmp_path, "a", **(a or {})),
            _zip_map(tmp_path, "b", chain=chain),
        ],
        extra_tasks=[_stage_map(tmp_path, "c", **(c or {}))],
        extra_edges=[
            _edge("split", "batches", "c", "in"),
            _edge("c", "out", "b", "mates"),
        ],
    )


def _starts(tmp_path: Path) -> set[str]:
    """The clone keys that logged a start, then clear the log."""
    log = tmp_path / "log"
    if not log.exists():
        return set()
    keys = {
        line.split()[0]
        for line in log.read_text().splitlines()
        if line.split()[1] == "start"
    }
    log.unlink()
    return keys


@pytest.mark.unit
class TestZip:
    """``zip``: slot X of each paired input reaches clone X."""

    async def test_zip_pairs_by_slot_without_chain(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """B[1] reads A[1] and C[1]."""
        del horus_context
        wf = _zipped(tmp_path, ["x", "y"])

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert _result(tmp_path, "b", "1") == (
            "y>a_out_1\ny>c_out_1\n>b_out_1\n"
        )

    async def test_zip_missing_slot_drops_item(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A slot C dropped drops B's item, with C as the origin; a slot only
        C has (A dropped it) is a gap, not an error.
        """
        _set(tmp_path, "exit", "c_out_1", 3)
        _set(tmp_path, "exit", "a_out_2", 3)
        wf = _zipped(
            tmp_path,
            ["x", "y", "z"],
            a={"drop_exit_codes": [3]},
            c={"drop_exit_codes": [3]},
        )

        with patch.object(
            horus_context.bus, "emit", wraps=horus_context.bus.emit
        ) as emit:
            await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert _clone_states(wf, "b") == {
            "b[0]": TaskStatus.COMPLETED,
            "b[1]": (TaskStatus.SKIPPED, SkipReason.UPSTREAM_DROPPED),
        }
        assert sorted(p.name for p in (tmp_path / "b_out").iterdir()) == ["0"]
        (event,) = [e for e in _drop_events(emit) if e.task_id == "b"]
        assert event.data["map"]["upstream_dropped"] == {"1": "c"}

    async def test_zip_folder_unfingerprinted_on_clones(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A clone fingerprints its paired item, not the whole folder."""
        del horus_context
        wf = _zipped(tmp_path, ["x", "y"])

        await wf.run(trigger_id="split")

        clone = next(t for t in wf.tasks if t.id == "b[0]")
        assert sorted(clone.unfingerprinted_inputs) == ["in", "mates"]
        assert sorted(a.id for a in clone.fingerprinted_inputs) == [
            "item",
            "mate",
        ]

    async def test_one_zip_item_change_reruns_one_clone(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A changed slot of C re-runs only the B clone paired with it."""
        del horus_context
        await _zipped(tmp_path, ["x", "y", "z"]).run(trigger_id="split")
        _starts(tmp_path)
        (tmp_path / "c_out" / "1" / "result.txt").write_text("changed")

        wf = _zipped(tmp_path, ["x", "y", "z"])
        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert _starts(tmp_path) == {"b_out_1"}
        assert _result(tmp_path, "b", "1") == ("y>a_out_1\nchanged>b_out_1\n")

    async def test_chained_zip_pairs_by_slot(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Chained, B[0] starts on A[0] and C[0] while A[2] still runs."""
        del horus_context
        _set(tmp_path, "delay", "a_out_2", 0.5)
        wf = _zipped(tmp_path, ["x", "y", "z"], chain=True)

        await wf.run(trigger_id="split")

        assert wf.status.value == "completed"
        assert _when(tmp_path, "b_out_0", "start") < _when(
            tmp_path, "a_out_2", "end"
        )
        assert _result(tmp_path, "b", "2") == (
            "z>a_out_2\nz>c_out_2\n>b_out_2\n"
        )

    async def test_extra_zip_slot_from_a_late_drop_is_a_gap(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        H drops slot 1 at its end. C, chained, opened with H's full list;
        A, not chained, started after H with the gap. B, chained over A and
        pairing C, ignores C's slot 1 instead of failing.
        """
        del horus_context
        _set(tmp_path, "exit", "h_out_1", 3)
        _set(tmp_path, "delay", "h_out_2", 0.3)
        b = _zip_map(tmp_path, "b", chain=True)
        wf = _stages(
            tmp_path,
            ["x", "y", "z"],
            [
                _stage_map(tmp_path, "h", drop_exit_codes=[3]),
                _stage_map(tmp_path, "a"),
                b,
            ],
            extra_tasks=[_stage_map(tmp_path, "c", chain=True)],
            extra_edges=[
                _edge("h", "out", "c", "in"),
                _edge("c", "out", "b", "mates"),
            ],
        )

        await asyncio.wait_for(wf.run(trigger_id="split"), timeout=10)

        assert wf.status.value == "completed"
        assert _clone_states(wf, "c")["c[1]"] == (
            TaskStatus.SKIPPED,
            SkipReason.UPSTREAM_DROPPED,
        )
        assert _clone_states(wf, "b") == {
            "b[0]": TaskStatus.COMPLETED,
            "b[2]": TaskStatus.COMPLETED,
        }
        assert _result(tmp_path, "b", "2").startswith("z>h_out_2")

    def test_r1_slot_missing_from_a_stream(self) -> None:
        """Waiting on a slot the stream never had is a bug guard."""

        async def wait() -> None:
            stream = _ItemStream()
            stream.open({"0": "x"}, {}, LocalTarget())
            await stream.wait("1", owner="b", source="c")

        with pytest.raises(
            MapConfigurationError,
            match=re.escape(
                "Map task 'b' expected item 1 from 'c', which has no such "
                "item."
            ),
        ):
            asyncio.run(wait())


@pytest.fixture
def bound_failures() -> Any:
    """What the bound-task middleware raises, by task id."""
    failures: dict[str, BaseException] = {}
    original = list(BoundTaskMiddleware.registry)

    class _Raise(BoundTaskMiddleware):
        async def before(self, context: BoundTaskMiddlewareContext) -> None:
            exc = failures.get(context.task.id)
            if exc is not None:
                raise exc

    try:
        yield failures
    finally:
        BoundTaskMiddleware.registry = original


class _Cluster(LocalTarget):
    """A shared filesystem that is not the orchestrator's."""

    add_to_registry: ClassVar[bool] = False
    kind: str = "test_map_cluster"

    @property
    def location_id(self) -> str:
        """One location for every task on it."""
        return "fake://cluster"
