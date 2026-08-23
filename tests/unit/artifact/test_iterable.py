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
Unit tests for the IterableArtifact contract: Folder and JSON artifacts
enumerate deterministic items, reading strictly through the target's
channels (the regression proof for remote targets).
"""

import dataclasses
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import PrivateAttr

from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.artifact.json import JSONArtifact
from horus_builtin.target.local import LocalTarget
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.iterable import (
    ArtifactItem,
    ArtifactIterationError,
    IterableArtifact,
)
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.target.channel import (
    ChannelProcess,
    JobHandle,
    RemoteDirEntry,
)


class _ChannelOnlyTarget(BaseTarget):
    """
    A target whose filesystem is NOT the orchestrator's: it serves a canned
    tree purely over the channel and records every ``get_file``/
    ``list_dir`` call. Any local filesystem read by ``items()`` would miss
    these trees entirely (the local paths hold different content), so a
    passing assertion against the canned content proves the reads went over
    the channel.
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "_channel_only"
    kind_name: ClassVar[str] = "Channel Only"
    kind_description: ClassVar[str] = "Test stub serving a canned tree."

    _tree: dict[str, bytes | None] = PrivateAttr(default_factory=dict)
    _calls: list[tuple[str, str]] = PrivateAttr(default_factory=list)

    @property
    def location_id(self) -> str:
        return "channel://only"

    def path_on_target(self, artifact: BaseArtifact) -> str:
        """Map every artifact onto this target's own ``/remote`` root."""
        return f"/remote{artifact.path.as_posix()}"

    def access_cost(self, artifact: BaseArtifact) -> float | None:
        del artifact
        return None  # not accessible on the orchestrator fs

    async def run_command_sync(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ChannelProcess:
        raise NotImplementedError

    async def launch(
        self,
        cmd: str,
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        job_dir: str,
    ) -> JobHandle:
        raise NotImplementedError

    async def poll(self, handle: JobHandle) -> int | None:
        raise NotImplementedError

    async def read_output(self, handle: JobHandle) -> tuple[bytes, bytes]:
        raise NotImplementedError

    async def send_signal(self, handle: JobHandle, sig: int) -> None:
        raise NotImplementedError

    async def put_file(
        self, content: bytes | Path, remote_path: str
    ) -> None: ...

    async def get_file(self, remote_path: str) -> bytes:
        self._calls.append(("get_file", remote_path))
        data = self._tree[remote_path]
        assert data is not None
        return data

    async def mkdir(self, path: str) -> None:
        self._tree.setdefault(path, None)

    async def list_dir(self, path: str) -> list[RemoteDirEntry]:
        self._calls.append(("list_dir", path))
        parent = Path(path)
        out: list[RemoteDirEntry] = []
        for entry_path, data in self._tree.items():
            p = Path(entry_path)
            if p != parent and p.parent == parent:
                out.append(
                    RemoteDirEntry(
                        name=p.name,
                        path=entry_path,
                        is_dir=data is None,
                        size=0 if data is None else len(data),
                    )
                )
        return out

    def plant_file(self, path: str, content: bytes) -> None:
        """Serve *content* at *path* over the channel."""
        self._tree[path] = content

    @property
    def calls(self) -> list[tuple[str, str]]:
        """Recorded ``(operation, path)`` channel reads."""
        return self._calls


@pytest.mark.unit
class TestRegistryContract:
    """IterableArtifact is a mixin the builtin kinds opt into."""

    def test_folder_and_json_are_iterable(self) -> None:
        """The trait derivation (issubclass) holds for both kinds."""
        assert issubclass(FolderArtifact, IterableArtifact)
        assert issubclass(JSONArtifact, IterableArtifact)


@pytest.mark.unit
class TestFolderItems:
    """FolderArtifact.items enumerates children, sorted, zero copy."""

    async def test_items_against_local_target(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """One item per child; slot is the child name; path is absolute."""
        del horus_context
        base = tmp_path / "batches"
        base.mkdir()
        for name in ("c.txt", "a.txt", "b.txt"):
            (base / name).write_text(name)

        artifact = FolderArtifact(id="batches", path=base)
        items = await artifact.items(LocalTarget())

        assert [(i.slot, i.path) for i in items] == [
            ("a.txt", (base / "a.txt").as_posix()),
            ("b.txt", (base / "b.txt").as_posix()),
            ("c.txt", (base / "c.txt").as_posix()),
        ]
        assert all(i.value is None for i in items)

    async def test_empty_folder_yields_no_items(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """An empty folder iterates to an empty list."""
        del horus_context
        base = tmp_path / "empty"
        base.mkdir()

        artifact = FolderArtifact(id="batches", path=base)
        assert await artifact.items(LocalTarget()) == []

    async def test_children_listed_over_the_channel(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """On a remote-style target the listing goes over the channel and
        item paths are the *target-side* entry paths.
        """
        del horus_context
        # Decoy local content that must never be read.
        local = tmp_path / "local_batches"
        local.mkdir()
        (local / "decoy.txt").write_text("LOCAL")

        target = _ChannelOnlyTarget()
        remote = f"/remote{local.as_posix()}"
        target.plant_file(f"{remote}/zulu.txt", b"z")
        target.plant_file(f"{remote}/alpha.txt", b"a")

        artifact = FolderArtifact(id="batches", path=local)
        items = await artifact.items(target)

        assert [(i.slot, i.path) for i in items] == [
            ("alpha.txt", f"{remote}/alpha.txt"),
            ("zulu.txt", f"{remote}/zulu.txt"),
        ]
        assert target.calls == [("list_dir", remote)]


@pytest.mark.unit
class TestJSONItems:
    """JSONArtifact.items parses the list off the target and indexes slots."""

    async def test_items_against_local_target(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Zero-padded index slots carrying the parsed elements as values."""
        del horus_context
        artifact = JSONArtifact(id="batches", path=tmp_path / "b.json")
        artifact.write(["x", {"k": 1}, 3])

        items = await artifact.items(LocalTarget())

        assert [i.slot for i in items] == ["0", "1", "2"]
        assert [i.value for i in items] == ["x", {"k": 1}, 3]
        assert all(i.path is None for i in items)

    async def test_slots_are_zero_padded(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """11 elements pad every slot to the widest index's width."""
        del horus_context
        artifact = JSONArtifact(id="batches", path=tmp_path / "b.json")
        artifact.write(list(range(11)))

        items = await artifact.items(LocalTarget())

        assert items[0].slot == "00"
        assert items[-1].slot == "10"

    async def test_empty_list_yields_no_items(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """An empty JSON list iterates to an empty list."""
        del horus_context
        artifact = JSONArtifact(id="batches", path=tmp_path / "b.json")
        artifact.write([])

        assert await artifact.items(LocalTarget()) == []

    async def test_document_fetched_over_the_channel(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """On a remote-style target the JSON comes from the channel, not
        from the local file: regression proof against the old local-only
        ``read()`` behaviour.
        """
        del horus_context
        # Decoy local content that must never be read.
        local = tmp_path / "decoy.json"
        local.write_text('["LOCAL"]')

        target = _ChannelOnlyTarget()
        remote = f"/remote{local.as_posix()}"
        target.plant_file(remote, b'["a", "b"]')

        artifact = JSONArtifact(id="batches", path=local)
        items = await artifact.items(target)

        assert [i.value for i in items] == ["a", "b"]
        assert target.calls == [("get_file", remote)]

    async def test_non_list_document_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A JSON object is not iterable."""
        del horus_context
        artifact = JSONArtifact(id="batches", path=tmp_path / "b.json")
        artifact.write({"not": "a list"})

        with pytest.raises(ArtifactIterationError, match="JSON list"):
            await artifact.items(LocalTarget())

    async def test_malformed_document_raises(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Unparseable target-side documents raise ArtifactIterationError
        rather than leaking a raw JSONDecodeError.
        """
        del horus_context
        artifact = JSONArtifact(id="batches", path=tmp_path / "b.json")
        artifact.path.write_text("{nope")

        with pytest.raises(ArtifactIterationError, match="valid JSON"):
            await artifact.items(LocalTarget())


@pytest.mark.unit
class TestArtifactItemShape:
    """ArtifactItem carries exactly one of path/value."""

    def test_defaults_are_none(self) -> None:
        """A bare item has neither path nor value."""
        item = ArtifactItem(slot="0")
        assert item.path is None
        assert item.value is None

    def test_is_immutable(self) -> None:
        """Items are frozen: consumers cannot repoint one in place."""
        item = ArtifactItem(slot="0", path="/x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            item.path = "/y"  # type: ignore[misc]
