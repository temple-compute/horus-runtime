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
Unit tests for the run's digest cache (:meth:`BaseWorkflow.digest`): one
read per input, path and cache mode per run, dropped where a task writes.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.store import (
    SMALL_FILE,
    ArtifactStore,
    CacheMode,
)
from horus_runtime.core.target.channel import ChannelProcess
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.exceptions import TargetLostError
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.status import WorkflowStatus

_MIB = 1 << 20


def _running() -> HorusWorkflow:
    """A workflow that caches digests, as during a run."""
    wf = HorusWorkflow(name="cache", tasks=[])
    wf.status = WorkflowStatus.RUNNING
    return wf


def _file(path: str) -> FileArtifact:
    return FileArtifact(id="a", path=Path(path))


@pytest.mark.unit
class TestDigestCache:
    """:meth:`BaseWorkflow.digest` reads each key once while running."""

    async def test_concurrent_readers_share_one_read(self) -> None:
        """Readers of one key await the first one's read."""
        wf, target = _running(), LocalTarget()
        gate = asyncio.Event()
        reads: list[CacheMode] = []

        async def gated(
            store: ArtifactStore, artifact: BaseArtifact, mode: CacheMode
        ) -> str | None:
            del store, artifact
            reads.append(mode)
            await gate.wait()
            return "d"

        with patch.object(ArtifactStore, "digest", gated):
            readers = [
                asyncio.create_task(wf.digest(target, _file("/a"), "fast"))
                for _ in range(3)
            ]
            await asyncio.sleep(0)
            gate.set()
            assert await asyncio.gather(*readers) == ["d"] * 3
            assert await wf.digest(target, _file("/a"), "fast") == "d"

        assert reads == ["fast"]

    async def test_none_is_not_kept(self) -> None:
        """An input that could not be digested is read again next time."""
        wf, target = _running(), LocalTarget()
        reads: list[str] = []

        async def missing(
            store: ArtifactStore, artifact: BaseArtifact, mode: CacheMode
        ) -> str | None:
            del store, artifact
            reads.append(mode)
            return None

        with patch.object(ArtifactStore, "digest", missing):
            assert await wf.digest(target, _file("/a"), "deep") is None
            assert await wf.digest(target, _file("/a"), "deep") is None

        assert reads == ["deep", "deep"]

    async def test_an_error_is_not_kept_and_waiters_retry(self) -> None:
        """The reader that fails raises; the ones waiting on it read again."""
        wf, target = _running(), LocalTarget()
        gate = asyncio.Event()
        reads: list[str] = []

        async def lost_once(
            store: ArtifactStore, artifact: BaseArtifact, mode: CacheMode
        ) -> str | None:
            del store, artifact
            reads.append(mode)
            await gate.wait()
            if len(reads) == 1:
                raise TargetLostError("node gone")
            return "d"

        with patch.object(ArtifactStore, "digest", lost_once):
            readers = [
                asyncio.create_task(wf.digest(target, _file("/a"), "deep"))
                for _ in range(3)
            ]
            await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*readers, return_exceptions=True)
            assert await wf.digest(target, _file("/a"), "deep") == "d"

        assert isinstance(results[0], TargetLostError)
        assert results[1:] == ["d", "d"]
        assert len(reads) == 2

    async def test_forget_drops_paths_around_a_write(self) -> None:
        """
        Equal, under and above the path go, in every generation and mode;
        siblings and other locations stay.
        """
        wf, target = _running(), LocalTarget()
        here = target.location_id
        kept = [
            (here, 0, "/a/c", "deep"),
            (here, 0, "/ab", "deep"),
            (here, 0, "/x", "deep"),
            ("elsewhere", 0, "/a/b", "deep"),
        ]
        dropped = [
            (here, 0, "/", "deep"),
            (here, 0, "/a", "deep"),
            (here, 0, "/a/b", "fast"),
            (here, 3, "/a/b", "deep"),
            (here, 0, "/a/b/c", "deep"),
        ]
        for key in kept + dropped:
            future = asyncio.get_running_loop().create_future()
            future.set_result("d")
            wf._digests[cast(Any, key)] = future

        wf.forget_digests(target, ["/a/b/"])

        assert sorted(wf._digests) == sorted(kept)

    async def test_seeded_digest_is_read_without_a_command(self) -> None:
        """A seeded digest answers readers of its key."""
        wf, target = _running(), LocalTarget()
        wf.seed_digest(target, _file("/a"), "fast", "fast:seeded")

        with patch.object(
            ArtifactStore, "digest", side_effect=AssertionError("read")
        ):
            assert await wf.digest(target, _file("/a"), "fast") == (
                "fast:seeded"
            )

    async def test_pass_through_outside_a_run(self) -> None:
        """Outside a run every call reads and nothing is seeded."""
        wf, target = HorusWorkflow(name="idle", tasks=[]), LocalTarget()
        reads: list[str] = []

        async def reading(
            store: ArtifactStore, artifact: BaseArtifact, mode: CacheMode
        ) -> str | None:
            del store, artifact
            reads.append(mode)
            return "d"

        wf.seed_digest(target, _file("/a"), "deep", "seeded")
        with patch.object(ArtifactStore, "digest", reading):
            assert await wf.digest(target, _file("/a"), "deep") == "d"
            assert await wf.digest(target, _file("/a"), "deep") == "d"

        assert reads == ["deep", "deep"]
        assert wf._digests == {}


def _consumers(
    work: Path,
    data: Path,
    modes: list[CacheMode | None],
    *,
    cache: CacheMode = "deep",
    writer_after: int | None = None,
) -> HorusWorkflow:
    """
    ``start``, then one consumer ``c<i>`` of the root folder *data* per
    entry of *modes* (its own ``cache``), each writing one output under
    *work*. With *writer_after*, a task ``w`` adds a file to *data* after
    ``c<writer_after>`` and before every later consumer.
    """
    work.mkdir(exist_ok=True)
    target = LocalTarget(working_directory=work.as_posix())
    start = HorusTask(
        id="start",
        name="start",
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=target,
    )
    consumers = [
        HorusTask(
            id=f"c{i}",
            name=f"c{i}",
            cache=mode,
            inputs=[FolderArtifact(id="inp", path=Path("data"))],
            outputs=[FileArtifact(id="out", path=work / f"c{i}.txt")],
            runtime=CommandRuntime(command=f"echo {i} > {work}/c{i}.txt"),
            executor=ShellExecutor(),
            target=target,
        )
        for i, mode in enumerate(modes)
    ]
    edges = [
        edge
        for task in consumers
        for edge in (
            WorkflowEdge(source="start", target=task.id),
            WorkflowEdge(
                source="artifact-data",
                source_output="data",
                target=task.id,
                target_input="inp",
            ),
        )
    ]
    tasks: list[BaseTask] = [start, *consumers]
    if writer_after is not None:
        writer = HorusTask(
            id="w",
            name="w",
            outputs=[FileArtifact(id="out", path=data / "new.txt")],
            runtime=CommandRuntime(command=f"echo new > {data}/new.txt"),
            executor=ShellExecutor(),
            target=target,
        )
        tasks.append(writer)
        edges += [WorkflowEdge(source=f"c{writer_after}", target="w")] + [
            WorkflowEdge(source="w", target=task.id)
            for task in consumers[writer_after + 1 :]
        ]
    return HorusWorkflow(
        name="folder_consumers",
        tasks=tasks,
        edges=edges,
        artifacts=[FolderArtifact(id="data", path=data)],
        orchestrator_target=target,
        cache=cache,
    )


def _statuses(wf: HorusWorkflow) -> dict[str, TaskStatus]:
    return {t.id: t.status for t in wf.tasks if t.id.startswith("c")}


def _manifest_digest(work: Path, task_id: str) -> str:
    manifest = json.loads((work / ".horus" / f"{task_id}.json").read_text())
    return cast(str, manifest["inputs"]["inp"])


@pytest.mark.unit
class TestRunCache:
    """Whole runs read each input once and see every write."""

    async def test_cleared_after_the_run(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Nothing read in a run is kept for the next one."""
        del horus_context
        data = tmp_path / "data"
        data.mkdir()
        (data / "f.txt").write_text("x")
        wf = _consumers(tmp_path / "work", data, [None])
        seen: list[int] = []
        digest = BaseWorkflow.digest

        async def watching(
            self: BaseWorkflow,
            target: Any,
            artifact: BaseArtifact,
            mode: CacheMode,
        ) -> str | None:
            value = await digest(self, target, artifact, mode)
            seen.append(len(self._digests))
            return value

        with patch.object(BaseWorkflow, "digest", watching):
            await asyncio.wait_for(wf.run(trigger_id="start"), timeout=30)

        assert wf.status == WorkflowStatus.COMPLETED
        assert seen and max(seen) == 1
        assert wf._digests == {}

    async def test_in_place_writer_invalidates(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A consumer after a task that wrote into its input reads anew."""
        del horus_context
        data, work = tmp_path / "data", tmp_path / "work"
        data.mkdir()
        (data / "f.txt").write_text("x")
        wf = _consumers(work, data, [None, None], writer_after=0)

        await asyncio.wait_for(wf.run(trigger_id="start"), timeout=30)

        assert wf.status == WorkflowStatus.COMPLETED
        before, after = (_manifest_digest(work, c) for c in ("c0", "c1"))
        assert before != after
        assert after == await ArtifactStore(LocalTarget()).digest(
            FolderArtifact(id="data", path=data)
        )

    async def test_digest_taken_while_writer_runs_is_dropped(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        ``r1`` digests the folder while ``w`` is still running, before it
        writes; ``r2``, after ``w``, must not reuse that digest.
        """
        del horus_context
        data, work = tmp_path / "data", tmp_path / "work"
        data.mkdir()
        work.mkdir()
        (data / "f.txt").write_text("x")
        started, go = tmp_path / "started", tmp_path / "go"
        target = LocalTarget(working_directory=work.as_posix())

        def task(
            task_id: str, command: str, out: Path, *, reads: bool = False
        ) -> HorusTask:
            inputs: list[BaseArtifact] = [
                FolderArtifact(id="inp", path=Path("data"))
            ]
            return HorusTask(
                id=task_id,
                name=task_id,
                inputs=inputs if reads else [],
                outputs=[FileArtifact(id="out", path=out)],
                runtime=CommandRuntime(command=command),
                executor=ShellExecutor(),
                target=target,
            )

        wait = "while [ ! -f {0} ]; do sleep 0.05; done"
        tasks: list[BaseTask] = [
            task("start", "true", work / "start.txt"),
            # Writes only once r1 has digested the folder and run.
            task(
                "w",
                f"touch {started}; {wait.format(go)}; echo n > {data}/n.txt",
                data / "n.txt",
            ),
            # Holds r1 back until w runs, past w's dispatch.
            task("d", wait.format(started), work / "d.txt"),
            task("r1", f"touch {go} {work}/r1", work / "r1", reads=True),
            task("r2", f"touch {work}/r2", work / "r2", reads=True),
        ]
        order = [("start", "w"), ("start", "d"), ("d", "r1")]
        order += [("w", "r2"), ("r1", "r2")]
        edges = [WorkflowEdge(source=a, target=b) for a, b in order] + [
            WorkflowEdge(
                source="artifact-data",
                source_output="data",
                target=reader,
                target_input="inp",
            )
            for reader in ("r1", "r2")
        ]
        wf = HorusWorkflow(
            name="writer_running",
            tasks=tasks,
            edges=edges,
            artifacts=[FolderArtifact(id="data", path=data)],
            orchestrator_target=target,
        )

        await asyncio.wait_for(wf.run(trigger_id="start"), timeout=30)

        assert wf.status == WorkflowStatus.COMPLETED
        before, after = (_manifest_digest(work, r) for r in ("r1", "r2"))
        assert before != after
        assert after == await ArtifactStore(LocalTarget()).digest(
            FolderArtifact(id="data", path=data)
        )

    async def test_deep_and_fast_consumers_invalidate_differently(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A same-size rewrite with the mtime put back re-runs only the deep
        consumer; a new mtime re-runs only the fast one.
        """
        del horus_context
        data, work = tmp_path / "data", tmp_path / "work"
        data.mkdir()
        big = data / "big.bin"
        big.write_bytes(b"a" * 2 * _MIB)

        async def run() -> dict[str, TaskStatus]:
            wf = _consumers(work, data, ["deep", "fast"])
            await asyncio.wait_for(wf.run(trigger_id="start"), timeout=30)
            return _statuses(wf)

        done, skipped = TaskStatus.COMPLETED, TaskStatus.SKIPPED
        assert await run() == {"c0": done, "c1": done}

        stat = big.stat()
        big.write_bytes(b"b" * 2 * _MIB)
        os.utime(big, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert await run() == {"c0": done, "c1": skipped}

        os.utime(big, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        assert await run() == {"c0": skipped, "c1": done}


@pytest.mark.unit
class TestBytesRead:
    """
    How many digest commands, and how many input bytes they read, N
    consumers of one 8 MiB folder cost, without and with the run cache.
    """

    N = 6

    @staticmethod
    async def _measure(
        work: Path, data: Path, mode: CacheMode
    ) -> tuple[int, int, int]:
        """One run: (digest calls, bytes they read, digest commands)."""
        calls = size = commands = 0
        digest = ArtifactStore.digest
        run_command_sync = LocalTarget.run_command_sync

        async def measuring(
            store: ArtifactStore, artifact: BaseArtifact, mode: CacheMode
        ) -> str | None:
            nonlocal calls, size
            calls += 1
            root = store.target.path_on_target(artifact)
            for folder, _, names in os.walk(root):
                for name in names:
                    path = os.path.join(folder, name)
                    if os.path.isfile(path) and not os.path.islink(path):
                        n = os.path.getsize(path)
                        size += n if mode == "deep" or n < SMALL_FILE else 0
            return await digest(store, artifact, mode)

        async def counting(
            target: LocalTarget, cmd: str, **kwargs: Any
        ) -> ChannelProcess:
            nonlocal commands
            commands += "horus-tree" in cmd
            return await run_command_sync(target, cmd, **kwargs)

        wf = _consumers(work, data, [None] * TestBytesRead.N, cache=mode)
        with (
            patch.object(ArtifactStore, "digest", measuring),
            patch.object(LocalTarget, "run_command_sync", counting),
        ):
            await asyncio.wait_for(wf.run(trigger_id="start"), timeout=60)
        assert wf.status == WorkflowStatus.COMPLETED
        return calls, size, commands

    @pytest.mark.parametrize("mode", ["deep", "fast"])
    async def test_bytes_read(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        mode: CacheMode,
    ) -> None:
        """
        Before (no run cache): N commands reading N x 8 MiB in deep. After:
        1 command, 8 MiB in deep and 0 bytes in fast; the same when every
        consumer skips.
        """
        del horus_context
        data = tmp_path / "data"
        data.mkdir()
        for i in range(4):
            (data / f"f{i}.bin").write_bytes(bytes([i]) * 2 * _MIB)
        folder = 8 * _MIB
        deep = mode == "deep"

        async def uncached(
            self: BaseWorkflow,
            target: Any,
            artifact: BaseArtifact,
            mode: CacheMode,
        ) -> str | None:
            del self
            return await ArtifactStore(target).digest(artifact, mode)

        with patch.object(BaseWorkflow, "digest", uncached):
            before = [
                await self._measure(tmp_path / "b", data, mode)
                for _ in range(2)
            ]
        after = [
            await self._measure(tmp_path / "a", data, mode) for _ in range(2)
        ]

        for label, runs in (("before", before), ("after", after)):
            for run, (_calls, size, commands) in zip(
                ("run", "skip"), runs, strict=True
            ):
                print(  # noqa: T201 (the coordinator reports these numbers)
                    f"bytes-read {mode} {label} {run}: {commands} commands,"
                    f" {size} bytes ({size / _MIB:g} MiB)"
                )
        n = self.N
        assert before == [(n, n * folder if deep else 0, n)] * 2
        assert after == [(1, folder if deep else 0, 1)] * 2


# Every output gets the same mtime, so a rewrite of the same size lands in
# the same second as the last one (fast sees no change in its stat).
_SAME_SECOND = "touch -t 202301011200 $out"

_PRODUCER_YAML = """
name: producer_ran
kind: horus_workflow
cache: {mode}
artifacts: [{{id: seed, kind: file, path: seed.txt}}]
tasks:
  - id: p
    name: p
    kind: horus_task
    runtime: {{kind: command, command: "{body} > $out && {touch}"}}
    executor: {{kind: shell}}
    inputs: [{{kind: file, id: seed_in, path: seed.txt}}]
    outputs: [{{kind: file, id: out, path: p.bin}}]
  - id: c
    name: c
    kind: horus_task
    runtime: {{kind: command, command: "head -c 1 $inp > $out"}}
    executor: {{kind: shell}}
    inputs: [{{kind: file, id: inp, path: p.bin}}]
    outputs: [{{kind: file, id: out, path: c.txt}}]
edges:
  - {{source: artifact-seed, source_output: seed,
     target: p, target_input: seed_in}}
  - {{source: p, source_output: out, target: c, target_input: inp}}
"""

_MAP_YAML = """
name: producer_ran_map
kind: horus_workflow
cache: fast
artifacts: [{id: raw, kind: folder, path: raw}]
tasks:
  - kind: horus_map
    id: proc
    name: proc
    over: {input_id: raw_in, as: sample}
    inputs: [{kind: folder, id: raw_in, path: raw}]
    outputs: [{kind: folder, id: processed, path: processed}]
    runtime:
      kind: command
      command: >-
        out=$processed/d.bin && { head -c 1 $sample; head -c 1048576
        /dev/zero; } > $out && touch -t 202301011200 $out
    executor: {kind: shell}
  - kind: horus_map
    id: post
    name: post
    chain: true
    over: {input_id: proc_in, as: item}
    inputs: [{kind: folder, id: proc_in, path: processed}]
    outputs: [{kind: folder, id: sizes, path: sizes}]
    runtime: {kind: command, command: "head -c 1 $item/d.bin > $sizes/x"}
    executor: {kind: shell}
  - id: c
    name: c
    kind: horus_task
    runtime: {kind: command, command: "ls $proc_in > $out"}
    executor: {kind: shell}
    inputs: [{kind: folder, id: proc_in, path: processed}]
    outputs: [{kind: file, id: out, path: c.txt}]
edges:
  - {source: artifact-raw, source_output: raw,
     target: proc, target_input: raw_in}
  - {source: proc, source_output: processed,
     target: post, target_input: proc_in}
  - {source: proc, source_output: processed, target: c, target_input: proc_in}
"""


async def _run_yaml(path: Path, trigger: str) -> dict[str, TaskStatus]:
    wf = BaseWorkflow.from_yaml(path)
    await asyncio.wait_for(wf.run(trigger_id=trigger), timeout=30)
    assert wf.status == WorkflowStatus.COMPLETED
    return {t.id: t.status for t in wf.tasks}


@pytest.mark.unit
class TestProducerRan:
    """
    In ``fast``, an input whose producer ran in this run counts as changed:
    a rewrite of the same size in the same second keeps its fast digest.
    """

    @pytest.mark.parametrize(
        ("mode", "body", "consumer"),
        [
            # New content, same size and mtime: deep reads it, fast trusts
            # that the producer ran.
            ("deep", "{ cat $seed_in; head -c 1048576 /dev/zero; }", "ran"),
            ("fast", "{ cat $seed_in; head -c 1048576 /dev/zero; }", "ran"),
            # The same bytes again: only deep can tell nothing changed.
            ("deep", "head -c 1048577 /dev/zero", "skipped"),
            ("fast", "head -c 1048577 /dev/zero", "ran"),
        ],
    )
    async def test_consumer_of_a_rerun_producer(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
        mode: CacheMode,
        body: str,
        consumer: str,
    ) -> None:
        """The producer re-runs on a new seed; its consumer follows in fast."""
        del horus_context
        workflow = tmp_path / "wf.yaml"
        workflow.write_text(
            _PRODUCER_YAML.format(mode=mode, body=body, touch=_SAME_SECOND)
        )
        seed = tmp_path / "seed.txt"
        seed.write_text("a")
        done, skipped = TaskStatus.COMPLETED, TaskStatus.SKIPPED
        assert await _run_yaml(workflow, "p") == {"p": done, "c": done}
        assert await _run_yaml(workflow, "p") == {"p": skipped, "c": skipped}

        before = (tmp_path / "p.bin").stat()
        seed.write_text("b")
        statuses = await _run_yaml(workflow, "p")

        after = (tmp_path / "p.bin").stat()
        assert (after.st_size, after.st_mtime) == (
            before.st_size,
            before.st_mtime,
        )
        expected = done if consumer == "ran" else skipped
        assert statuses == {"p": done, "c": expected}

    async def test_chained_clone_and_map_consumer(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A clone that rewrites its slot in the same second re-runs the
        chained clone of that slot and the map folder's consumer only.
        """
        del horus_context
        workflow = tmp_path / "wf.yaml"
        workflow.write_text(_MAP_YAML)
        raw = tmp_path / "raw"
        raw.mkdir()
        for name in ("s0", "s1"):
            (raw / name).write_text(name)
        assert all(
            s is TaskStatus.COMPLETED
            for s in (await _run_yaml(workflow, "proc")).values()
        )

        # A new first byte: fast reads a small item by content.
        (raw / "s1").write_text("t1")
        statuses = await _run_yaml(workflow, "proc")

        done, skipped = TaskStatus.COMPLETED, TaskStatus.SKIPPED
        clones = {k: v for k, v in statuses.items() if "[" in k}
        assert clones == {
            "proc[0]": skipped,
            "proc[1]": done,
            "post[0]": skipped,
            "post[1]": done,
        }
        assert statuses["c"] is done
