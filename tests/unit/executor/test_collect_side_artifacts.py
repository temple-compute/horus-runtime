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
Unit tests for ``BaseExecutor.collect_side_artifacts`` — collecting side
artifacts back to the orchestrator over the target channel.
"""

from pathlib import Path, PurePosixPath
from typing import ClassVar

import pytest
from pydantic import PrivateAttr

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.target.channel import ChannelProcess, RemoteDirEntry
from horus_runtime.settings import runtime_settings
from tests.conftest import MakeTaskType


@pytest.mark.unit
class TestCollectLocalSideArtifacts:
    """
    Collection from a ``LocalTarget``, exercising the full channel path
    (``list_dir`` + ``get_file`` into a temp landing dir).
    """

    async def test_collects_files_and_folders(
        self, make_shell_task: MakeTaskType
    ) -> None:
        """
        Top-level files become FileArtifacts and top-level folders become
        FolderArtifacts, with nested and empty directories reconstructed
        locally and the bytes transferred out of the source directory.
        """
        task = make_shell_task()
        sad = Path(task.side_artifacts_dir)
        sad.mkdir(parents=True)
        (sad / "log.txt").write_text("hello")
        (sad / "sub").mkdir()
        (sad / "sub" / "nested.txt").write_text("deep")
        (sad / "empty").mkdir()

        await task.executor.collect_side_artifacts(task)

        by_id = {a.id: a for a in task.side_artifacts}
        assert set(by_id) == {
            "test_task_id_log.txt",
            "test_task_id_sub",
            "test_task_id_empty",
        }

        log = by_id["test_task_id_log.txt"]
        assert isinstance(log, FileArtifact)
        assert log.path.read_text() == "hello"
        # Transferred: it landed somewhere other than the source dir.
        assert log.path.parent != sad.resolve()

        sub = by_id["test_task_id_sub"]
        assert isinstance(sub, FolderArtifact)
        assert (sub.path / "nested.txt").read_text() == "deep"

        empty = by_id["test_task_id_empty"]
        assert isinstance(empty, FolderArtifact)
        assert empty.path.is_dir()

    async def test_skips_files_over_cap(
        self, make_shell_task: MakeTaskType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Files larger than ``MAX_SIDE_ARTIFACT_BYTES`` are skipped; smaller
        ones are still collected.
        """
        monkeypatch.setattr(runtime_settings, "MAX_SIDE_ARTIFACT_BYTES", 10)
        task = make_shell_task()
        sad = Path(task.side_artifacts_dir)
        sad.mkdir(parents=True)
        (sad / "small.txt").write_text("ok")
        (sad / "big.txt").write_text("x" * 100)

        await task.executor.collect_side_artifacts(task)

        collected = {a.id for a in task.side_artifacts}
        assert collected == {"test_task_id_small.txt"}

    async def test_missing_dir_is_noop(
        self, make_shell_task: MakeTaskType
    ) -> None:
        """
        A missing side-artifacts dir collects nothing and does not raise.
        """
        task = make_shell_task()  # side_artifacts_dir never created
        await task.executor.collect_side_artifacts(task)
        assert task.side_artifacts == []


class _InMemoryRemoteTarget(BaseTarget):
    """
    A target whose filesystem is NOT the orchestrator's: it serves an in-memory
    tree over the channel, proving collection works with no shared filesystem.
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "_inmem_remote"
    # path -> bytes (file) or None (directory)
    _tree: dict[str, bytes | None] = PrivateAttr(default_factory=dict)

    @property
    def location_id(self) -> str:
        return "inmem://remote"

    def access_cost(self, _: BaseArtifact) -> float | None:
        return None  # not accessible on the orchestrator fs

    async def run_command(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ChannelProcess:
        raise NotImplementedError

    async def put_file(
        self, content: bytes | Path, remote_path: str
    ) -> None: ...

    async def get_file(self, remote_path: str) -> bytes:
        data = self._tree[remote_path]
        assert data is not None
        return data

    async def mkdir(self, path: str) -> None:
        self._tree.setdefault(path, None)

    async def list_dir(self, path: str) -> list[RemoteDirEntry]:
        parent = PurePosixPath(path)
        out: list[RemoteDirEntry] = []
        for entry_path, data in self._tree.items():
            pp = PurePosixPath(entry_path)
            if pp != parent and pp.parent == parent:
                out.append(
                    RemoteDirEntry(
                        name=pp.name,
                        path=entry_path,
                        is_dir=data is None,
                        size=0 if data is None else len(data),
                    )
                )
        return out


@pytest.mark.unit
class TestCollectRemoteSideArtifacts:
    """
    Collection from a non-local target, purely over the channel.
    """

    async def test_collects_from_remote_target_via_channel(self) -> None:
        """
        Files/folders described only by the channel are pulled to a real local
        path on the orchestrator and registered.
        """
        target = _InMemoryRemoteTarget(working_directory="/remote")
        task = HorusTask(
            name="t",
            id="job",
            inputs=[],
            outputs=[],
            runtime=CommandRuntime(command="noop"),
            executor=ShellExecutor(),
            target=target,
        )
        sad = task.side_artifacts_dir  # /remote/job/side-artifacts
        target._tree = {
            sad: None,
            f"{sad}/out.txt": b"remote-bytes",
            f"{sad}/sub": None,
            f"{sad}/sub/inner.bin": b"\x00\x01\x02",
        }

        await task.executor.collect_side_artifacts(task)

        by_id = {a.id: a for a in task.side_artifacts}
        assert set(by_id) == {"job_out.txt", "job_sub"}

        out = by_id["job_out.txt"]
        assert isinstance(out, FileArtifact)
        assert out.path.exists()  # landed on the local fs
        assert out.path.read_bytes() == b"remote-bytes"

        sub = by_id["job_sub"]
        assert isinstance(sub, FolderArtifact)
        assert (sub.path / "inner.bin").read_bytes() == b"\x00\x01\x02"


class _MaliciousRemoteTarget(BaseTarget):
    """
    A target whose channel returns entry names containing path separators /
    parent refs, to verify collection rejects them (no path traversal).
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "_malicious_remote"

    @property
    def location_id(self) -> str:
        return "inmem://malicious"

    def access_cost(self, _: BaseArtifact) -> float | None:
        return None

    async def run_command(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ChannelProcess:
        raise NotImplementedError

    async def put_file(
        self, content: bytes | Path, remote_path: str
    ) -> None: ...

    async def get_file(self, _remote_path: str) -> bytes:
        return b"evil"

    async def mkdir(self, path: str) -> None: ...

    async def list_dir(self, path: str) -> list[RemoteDirEntry]:
        if path.endswith("/sub"):
            # A nested traversal child, to exercise the _pull_tree guard.
            return [RemoteDirEntry("..", f"{path}/..", False, 4)]
        return [
            RemoteDirEntry("../evil.txt", f"{path}/../evil.txt", False, 4),
            RemoteDirEntry("sub", f"{path}/sub", True, 0),
        ]


@pytest.mark.unit
class TestCollectRejectsUnsafeNames:
    """
    Path-traversal protection against untrusted channel listings.
    """

    async def test_rejects_unsafe_entry_names(self) -> None:
        """
        Entries whose name is not a single path component are skipped, both at
        the top level and inside reconstructed folders.
        """
        target = _MaliciousRemoteTarget(working_directory="/remote")
        task = HorusTask(
            name="t",
            id="job",
            inputs=[],
            outputs=[],
            runtime=CommandRuntime(command="noop"),
            executor=ShellExecutor(),
            target=target,
        )

        await task.executor.collect_side_artifacts(task)

        by_id = {a.id: a for a in task.side_artifacts}
        # Top-level "../evil.txt" is rejected; only the safe folder remains.
        assert set(by_id) == {"job_sub"}
        sub = by_id["job_sub"]
        assert isinstance(sub, FolderArtifact)
        # The nested ".." child was rejected, so the folder stays empty.
        assert list(sub.path.iterdir()) == []
