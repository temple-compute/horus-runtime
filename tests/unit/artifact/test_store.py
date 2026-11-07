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
Unit tests for ArtifactStore and the target filesystem primitives it uses.
"""

import asyncio
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.target.local import LocalTarget
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.store import (
    SMALL_FILE,
    ArtifactStore,
    CacheMode,
    _parse_digest,
    _tree_digest,
    compose_folder_digest,
)
from horus_runtime.core.transfer.generic import GenericTransfer


class _FarLocalTarget(LocalTarget):
    """
    A local target that pretends to live on a distinct machine.

    It reuses the real local filesystem primitives but reports a unique
    ``location_id`` (so transfers do not short-circuit) and SSH-like
    ``path_on_target`` semantics (``working_directory/<name>``), letting tests
    exercise the full package -> get_file_to -> put_file -> unpackage path
    against the local filesystem.
    """

    add_to_registry = False

    @property
    def location_id(self) -> str:
        return f"far://{self.resolved_working_directory}"

    def path_on_target(self, artifact: BaseArtifact) -> str:
        return f"{self.resolved_working_directory}/{Path(artifact.path).name}"


@pytest.mark.unit
class TestLocalTargetFilesystemPrimitives:
    """
    LocalTarget must satisfy everything needed for the ArtifactStore.
    """

    async def test_path_exists_for_file_and_dir(self) -> None:
        """path_exists is True for files and directories, False otherwise."""
        target = LocalTarget()
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "f.txt"
            file_path.write_text("hi")

            assert await target.path_exists(str(file_path)) is True
            assert await target.path_exists(temp_dir) is True
            assert (
                await target.path_exists(str(Path(temp_dir) / "missing"))
                is False
            )

    async def test_remove_file(self) -> None:
        """Remove deletes a single file."""
        target = LocalTarget()
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "f.txt"
            file_path.write_text("hi")

            await target.remove(str(file_path))
            assert not file_path.exists()

    async def test_remove_directory_recursively(self) -> None:
        """Remove deletes a directory and its contents."""
        target = LocalTarget()
        with tempfile.TemporaryDirectory() as temp_dir:
            nested = Path(temp_dir) / "dir" / "nested"
            nested.mkdir(parents=True)
            (nested / "a.txt").write_text("A")

            await target.remove(str(Path(temp_dir) / "dir"))
            assert not (Path(temp_dir) / "dir").exists()

    async def test_remove_missing_path_is_noop(self) -> None:
        """Removing a missing path does not raise."""
        target = LocalTarget()
        with tempfile.TemporaryDirectory() as temp_dir:
            # Should not raise.
            await target.remove(str(Path(temp_dir) / "missing"))


@pytest.mark.unit
class TestArtifactStore:
    """
    ArtifactStore maps artifacts to target filesystem operations.
    """

    async def test_exists_true_for_existing_file(self) -> None:
        """Exists is True when the file artifact has materialized."""
        store = ArtifactStore(LocalTarget())
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "f.txt"
            path.write_text("hi")
            artifact = FileArtifact(id="a", path=path)

            assert await store.exists(artifact) is True

    async def test_exists_false_for_missing_file(self) -> None:
        """Exists is False when the file artifact is absent."""
        store = ArtifactStore(LocalTarget())
        artifact = FileArtifact(id="a", path=Path("/nonexistent/f.txt"))

        assert await store.exists(artifact) is False

    async def test_exists_true_for_existing_folder(self) -> None:
        """Exists is True for a materialized folder artifact (generic -e)."""
        store = ArtifactStore(LocalTarget())
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = FolderArtifact(id="a", path=Path(temp_dir))

            assert await store.exists(artifact) is True

    async def test_folder_digest_tracks_contents_and_names(
        self, tmp_path: Path
    ) -> None:
        """A folder digests by content, so a folder input can invalidate a
        task's fingerprint; it used to digest to None (a constant).
        """
        folder = tmp_path / "f"
        (folder / "sub").mkdir(parents=True)
        (folder / "a.txt").write_text("a")
        (folder / "sub" / "b.txt").write_text("b")
        store = ArtifactStore(LocalTarget())
        artifact = FolderArtifact(id="f", path=folder)

        first = await store.digest(artifact)
        assert first is not None and first.startswith("deep:")
        assert await store.digest(artifact) == first

        (folder / "sub" / "b.txt").write_text("B")
        edited = await store.digest(artifact)
        assert edited not in (None, first)

        (folder / "sub" / "b.txt").rename(folder / "sub" / "c.txt")
        assert await store.digest(artifact) not in (None, first, edited)

    async def test_digest_of_missing_path_is_none(
        self, tmp_path: Path
    ) -> None:
        """Nothing to hash means no digest, never a crash."""
        store = ArtifactStore(LocalTarget())
        missing = FolderArtifact(id="m", path=tmp_path / "missing")
        assert await store.digest(missing) is None

    async def test_delete_removes_file_and_emits_event(
        self, horus_context: HorusContext
    ) -> None:
        """Delete removes a file artifact from the target."""
        del horus_context
        store = ArtifactStore(LocalTarget())
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "f.txt"
            path.write_text("hi")
            artifact = FileArtifact(id="a", path=path)

            await store.delete(artifact)

            assert not path.exists()

    async def test_delete_removes_folder(
        self, horus_context: HorusContext
    ) -> None:
        """Delete removes a folder artifact recursively."""
        del horus_context
        store = ArtifactStore(LocalTarget())
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "dir"
            folder.mkdir()
            (folder / "a.txt").write_text("A")
            artifact = FolderArtifact(id="a", path=folder)

            await store.delete(artifact)

            assert not folder.exists()

    async def test_delete_missing_artifact_is_noop(
        self, horus_context: HorusContext
    ) -> None:
        """Deleting a missing artifact does not raise or emit."""
        del horus_context
        store = ArtifactStore(LocalTarget())
        artifact = FileArtifact(id="a", path=Path("/nonexistent/f.txt"))

        # Should not raise.
        await store.delete(artifact)

    async def test_package_file_is_identity(
        self, horus_context: HorusContext
    ) -> None:
        """Packaging a single-file artifact returns its own path unchanged."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "f.txt"
            path.write_text("hi")
            target = LocalTarget(working_directory=temp_dir)
            store = ArtifactStore(target)
            artifact = FileArtifact(id="a", path=path)

            pkg = await store.package(artifact)

            assert pkg == target.path_on_target(artifact)

    async def test_package_unpackage_folder_round_trip(
        self, horus_context: HorusContext
    ) -> None:
        """Packaging then unpackaging a folder restores its contents."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            src = base / "src" / "data"
            (src / "nested").mkdir(parents=True)
            (src / "a.txt").write_text("A")
            (src / "nested" / "b.txt").write_text("B")

            target = LocalTarget(working_directory=str(base))
            store = ArtifactStore(target)

            src_art = FolderArtifact(id="data", path=src)
            pkg = await store.package(src_art)
            assert Path(pkg).is_file()

            dest = base / "out" / "data"
            dest_art = FolderArtifact(id="data", path=dest)
            await store.unpackage(dest_art, pkg)

            assert (dest / "a.txt").read_text() == "A"
            assert (dest / "nested" / "b.txt").read_text() == "B"


_MODES: tuple[CacheMode, ...] = ("deep", "fast")
_BIG = 2 * SMALL_FILE


def _odd_tree(root: Path) -> Path:
    """
    A folder with nested and empty dirs, symlinks, a big file and names a
    line-based listing would break on.
    """
    (root / "sub" / "deeper").mkdir(parents=True)
    (root / "sub" / "hollow").mkdir()
    (root / "empty").mkdir()
    (root / "top.txt").write_text("top")
    (root / "big.bin").write_bytes(b"b" * _BIG)
    (root / "sub" / "deeper" / "x").write_text("x")
    (root / "sub" / "line\nbreak").write_text("n")
    (root / "sub" / "back\\slash and space").write_text("s")
    (root / os.fsdecode(b"raw\xff")).write_text("r")
    (root / "sub" / "link_dir").symlink_to(root / "sub" / "deeper")
    (root / "sub" / "link_file").symlink_to(root / "top.txt")
    (root / "top_link").symlink_to(root / "top.txt")
    return root


def _artifact(path: Path) -> BaseArtifact:
    """The artifact kind *path* is on disk."""
    if path.is_dir():
        return FolderArtifact(id="a", path=path)
    return FileArtifact(id="a", path=path)


async def _composed(store: ArtifactStore, root: Path, mode: CacheMode) -> str:
    """*root*'s digest composed from its entries', skipping symlinks as
    ``list_dir`` does.
    """
    children: dict[str | bytes, str] = {}
    for entry in os.scandir(root):
        if not entry.is_symlink():
            digest = await store.digest(_artifact(Path(entry.path)), mode)
            assert digest is not None
            children[entry.name] = digest
    return compose_folder_digest(mode, children)


def _shim(bin_dir: Path, name: str, flag: str) -> None:
    """A *name* on ``PATH`` that fails on *flag* and runs the real one."""
    real = shutil.which(name)
    assert real is not None
    shim = bin_dir / name
    shim.write_text(
        f'#!/bin/sh\nfor a; do [ "$a" = {flag} ] && exit 1; done\n'
        f'exec {real} "$@"\n'
    )
    shim.chmod(0o755)


@pytest.mark.unit
class TestDigestFormat:
    """Digest values in both cache modes: shape, Merkle composition, the
    fallback tool paths and what ``fast`` does and does not notice.
    """

    async def test_file_deep_digest_is_plain_sha256(
        self, tmp_path: Path
    ) -> None:
        """Deep file digests keep their old format, so records stay valid."""
        path = tmp_path / "f"
        path.write_bytes(b"content")
        store = ArtifactStore(LocalTarget())
        assert await store.digest(_artifact(path)) == (
            hashlib.sha256(b"content").hexdigest()
        )
        assert (await store.digest(_artifact(path), "fast")) == (
            "fast:" + hashlib.sha256(b"content").hexdigest()
        )

    @pytest.mark.parametrize("mode", _MODES)
    async def test_composed_equals_recomputed(
        self, tmp_path: Path, mode: CacheMode
    ) -> None:
        """What a map composes from its slots equals a fresh read of the
        folder, so either can stand for the other.
        """
        store = ArtifactStore(LocalTarget())
        root = _odd_tree(tmp_path / "root")
        whole = await store.digest(_artifact(root), mode)
        assert whole is not None and whole.startswith(f"{mode}:")
        assert await _composed(store, root, mode) == whole

        sub = root / "sub"
        assert await _composed(store, sub, mode) == (
            await store.digest(_artifact(sub), mode)
        )

        empty = tmp_path / "empty_root"
        empty.mkdir()
        assert await store.digest(_artifact(empty), mode) == (
            compose_folder_digest(mode, {})
        )

    def test_a_name_both_file_and_folder_is_a_miss(self) -> None:
        """
        A folder changed while it was walked can list a name as a file and
        a folder: a ``ValueError``, which ``digest`` turns into ``None``.
        """
        leaves = {b"./a": "0" * 64, b"./a/b": "1" * 64}
        with pytest.raises(ValueError, match="a/b"):
            _tree_digest("deep", leaves)

    def test_names_come_from_find_not_the_hasher(self) -> None:
        """
        The uutils ``sha256sum -z`` prints a non-UTF-8 name with U+FFFD in
        it; the batch's own name list keeps the real bytes.
        """
        hexd = "a" * 64
        record = hexd.encode() + b"  ./raw\xef\xbf\xbd"
        out = b"\0".join(
            [b"horus-tree", b"horus-names", b"1", b"./raw\xff", record, b""]
        )
        assert _parse_digest("deep", out) == (
            compose_folder_digest("deep", {b"raw\xff": hexd})
        )
        short = out.replace(b"\x001\x00", b"\x002\x00")
        with pytest.raises(ValueError):
            _parse_digest("deep", short)

    async def test_fallbacks_give_the_same_digests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without ``sha256sum -z`` and ``find -printf`` (old coreutils,
        busybox, macOS) the per-file loops give the same digests.
        """
        store = ArtifactStore(LocalTarget())
        root = _odd_tree(tmp_path / "root")
        big = _artifact(root / "big.bin")
        targets = [_artifact(root), big, _artifact(root / "top.txt")]
        expected = [await store.digest(a, m) for a in targets for m in _MODES]
        assert None not in expected

        shims = tmp_path / "shims"
        shims.mkdir()
        _shim(shims, "sha256sum", "-z")
        _shim(shims, "find", "-printf")
        path = os.environ["PATH"]
        monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{path}")
        got = [await store.digest(a, m) for a in targets for m in _MODES]
        assert got == expected

        busybox = shutil.which("busybox")
        if busybox is None:
            return
        applets = tmp_path / "busybox"
        applets.mkdir()
        for name in ("sha256sum", "find", "xargs", "stat", "cut", "sh"):
            (applets / name).symlink_to(busybox)
        monkeypatch.setenv("PATH", f"{applets}{os.pathsep}{path}")
        got = [await store.digest(a, m) for a in targets for m in _MODES]
        assert got == expected

    @pytest.mark.parametrize("in_folder", [False, True])
    async def test_fast_trades_content_for_size_and_mtime(
        self, tmp_path: Path, in_folder: bool
    ) -> None:
        """``fast`` sees size and whole-second mtime of big files, and the
        content of small ones; ``deep`` sees content only.
        """
        store = ArtifactStore(LocalTarget())
        big = tmp_path / "d" / "big.bin"
        small = tmp_path / "d" / "small.txt"
        big.parent.mkdir()
        big.write_bytes(b"a" * _BIG)
        small.write_text("one")
        second = 1_700_000_000 * 10**9
        for path in (big, small):
            os.utime(path, ns=(second, second + 1))
        artifact = _artifact(big.parent if in_folder else big)

        async def both() -> tuple[str | None, str | None]:
            return (
                await store.digest(artifact, "deep"),
                await store.digest(artifact, "fast"),
            )

        deep, fast = await both()

        big.write_bytes(b"z" * _BIG)
        os.utime(big, ns=(second, second + 1))
        deep2, fast2 = await both()
        assert deep2 != deep
        assert fast2 == fast

        os.utime(big, ns=(second, second + 999_999_999))
        assert await both() == (deep2, fast2)

        os.utime(big, ns=(second, second + 10**9))
        deep3, fast3 = await both()
        assert deep3 == deep2
        assert fast3 != fast2

        if in_folder:
            small.write_text("two")
            os.utime(small, ns=(second, second + 1))
            assert (await store.digest(artifact, "fast")) != fast3

    @pytest.mark.skipif(
        os.name != "posix" or os.geteuid() == 0, reason="root reads anyway"
    )
    @pytest.mark.parametrize("in_folder", [False, True])
    async def test_fast_never_reads_big_files(
        self, tmp_path: Path, in_folder: bool
    ) -> None:
        """An unreadable big file still digests in ``fast``: it is only
        stat'ed. ``deep`` has to read it, so it fails.
        """
        store = ArtifactStore(LocalTarget())
        big = tmp_path / "d" / "big.bin"
        big.parent.mkdir()
        big.write_bytes(b"a" * _BIG)
        big.chmod(0)
        artifact = _artifact(big.parent if in_folder else big)
        try:
            assert await store.digest(artifact, "fast") is not None
            assert await store.digest(artifact, "deep") is None
        finally:
            big.chmod(0o644)

    async def test_pack_unpack_keeps_the_fast_digest(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A folder transfer keeps whole-second mtimes, so a ``fast``
        consumer of the copy matches the source.
        """
        del horus_context
        src = tmp_path / "src" / "data"
        src.mkdir(parents=True)
        (src / "big.bin").write_bytes(b"a" * _BIG)
        (src / "small.txt").write_text("s")
        os.utime(src / "big.bin", ns=(1_700_000_000_123_456_789,) * 2)
        store = ArtifactStore(LocalTarget(working_directory=str(tmp_path)))
        src_art = FolderArtifact(id="data", path=src)
        dest_art = FolderArtifact(id="data", path=tmp_path / "out" / "data")

        await store.unpackage(dest_art, await store.package(src_art))

        for name in ("big.bin", "small.txt"):
            copied = (tmp_path / "out" / "data" / name).stat().st_mtime
            assert int(copied) == int((src / name).stat().st_mtime)
        assert await store.digest(dest_art, "fast") == (
            await store.digest(src_art, "fast")
        )


@pytest.mark.unit
class TestGenericTransfer:
    """
    GenericTransfer moves artifacts over any target pair via the store.
    """

    async def test_same_location_short_circuits(
        self, horus_context: HorusContext
    ) -> None:
        """Same location_id: no move, artifact repointed to path_on_target."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "f.txt"
            path.write_text("hi")
            src = LocalTarget(working_directory=temp_dir)
            dst = LocalTarget(working_directory=temp_dir)
            artifact = FileArtifact(id="a", path=path)

            await GenericTransfer().transfer(artifact, src, dst)

            assert artifact.path == Path(dst.path_on_target(artifact))

    async def test_cross_location_folder_transfer(
        self, horus_context: HorusContext
    ) -> None:
        """
        Distinct locations: a folder is packaged on the source, streamed, and
        unpackaged intact at the destination.
        """
        del horus_context
        with (
            tempfile.TemporaryDirectory() as src_dir,
            tempfile.TemporaryDirectory() as dst_dir,
        ):
            src_data = Path(src_dir) / "data"
            (src_data / "nested").mkdir(parents=True)
            (src_data / "a.txt").write_text("A")
            (src_data / "nested" / "b.txt").write_text("B")

            source = _FarLocalTarget(working_directory=src_dir)
            destination = _FarLocalTarget(working_directory=dst_dir)
            artifact = FolderArtifact(id="data", path=src_data)

            await GenericTransfer().transfer(artifact, source, destination)

            dest_dir_path = Path(dst_dir) / "data"
            assert (dest_dir_path / "a.txt").read_text() == "A"
            assert (dest_dir_path / "nested" / "b.txt").read_text() == "B"
            assert artifact.path == Path(destination.path_on_target(artifact))

    async def test_cross_location_file_transfer(
        self, horus_context: HorusContext
    ) -> None:
        """A single file transfers by identity across distinct locations."""
        del horus_context
        with (
            tempfile.TemporaryDirectory() as src_dir,
            tempfile.TemporaryDirectory() as dst_dir,
        ):
            src_file = Path(src_dir) / "f.txt"
            src_file.write_text("payload")

            source = _FarLocalTarget(working_directory=src_dir)
            destination = _FarLocalTarget(working_directory=dst_dir)
            artifact = FileArtifact(id="f", path=src_file)

            await GenericTransfer().transfer(artifact, source, destination)

            assert Path(dst_dir, "f.txt").read_text() == "payload"

    async def test_transfer_does_not_load_package_into_memory(
        self, horus_context: HorusContext
    ) -> None:
        """
        The package moves via get_file_to; get_file is never called.
        """
        del horus_context

        class _NoGetFileTarget(_FarLocalTarget):
            add_to_registry = False

            async def get_file(self, remote_path: str) -> bytes:
                raise AssertionError(f"get_file({remote_path}) was called")

        with (
            tempfile.TemporaryDirectory() as src_dir,
            tempfile.TemporaryDirectory() as dst_dir,
        ):
            src_data = Path(src_dir) / "data"
            src_data.mkdir()
            (src_data / "a.txt").write_text("A")

            source = _NoGetFileTarget(working_directory=src_dir)
            destination = _FarLocalTarget(working_directory=dst_dir)
            artifact = FolderArtifact(id="data", path=src_data)

            await GenericTransfer().transfer(artifact, source, destination)

            assert (Path(dst_dir) / "data" / "a.txt").read_text() == "A"

    async def test_concurrent_transfers_of_same_folder_package_once(
        self, horus_context: HorusContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Two consumers of one folder on the same machine starting at once
        share a single copy: one package, both repointed at it.
        """
        del horus_context
        packaged: list[str] = []
        package = ArtifactStore.package

        async def _counting_package(
            store: ArtifactStore, artifact: BaseArtifact
        ) -> str:
            packaged.append(artifact.id)
            return await package(store, artifact)

        monkeypatch.setattr(ArtifactStore, "package", _counting_package)

        with (
            tempfile.TemporaryDirectory() as src_dir,
            tempfile.TemporaryDirectory() as dst_dir,
        ):
            src_data = Path(src_dir) / "data"
            src_data.mkdir()
            (src_data / "a.txt").write_text("A")

            source = _FarLocalTarget(working_directory=src_dir)
            destination = _FarLocalTarget(working_directory=dst_dir)
            first = FolderArtifact(id="data", path=src_data)
            second = FolderArtifact(id="data", path=src_data)

            await asyncio.gather(
                GenericTransfer().transfer(first, source, destination),
                GenericTransfer().transfer(second, source, destination),
            )

            assert packaged == ["data"]
            assert first.path == second.path == Path(dst_dir) / "data"
            assert (Path(dst_dir) / "data" / "a.txt").read_text() == "A"
            assert not list(Path(src_dir).glob("*.horuspkg"))


@pytest.mark.unit
class TestTrailingSlashWorkingDirectory:
    """
    A trailing ``/`` on ``working_directory`` must not break transfers
    (``mv wd//f wd/f`` fails with "are the same file" on GNU mv).
    """

    def test_working_directory_trailing_slash_stripped(self) -> None:
        """Trailing slashes are dropped; the root stays ``/``."""
        assert LocalTarget(working_directory="/a/b/").working_directory == (
            "/a/b"
        )
        assert LocalTarget(working_directory="/a/b//").working_directory == (
            "/a/b"
        )
        assert LocalTarget(working_directory="/").working_directory == "/"

    async def test_unpackage_file_skips_self_move(
        self, horus_context: HorusContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A package path differing only by ``//`` is already in place."""
        del horus_context
        with tempfile.TemporaryDirectory() as tmp:
            # Resolved, since FileArtifact resolves its path (macOS /private).
            temp_dir = str(Path(tmp).resolve())
            path = Path(temp_dir) / "f.txt"
            path.write_text("hi")
            store = ArtifactStore(LocalTarget(working_directory=temp_dir))
            artifact = FileArtifact(id="a", path=path)

            async def _no_command(*_: object, **__: object) -> None:
                raise AssertionError("unpackage must not run a command")

            monkeypatch.setattr(store, "_run", _no_command)
            await store.unpackage(artifact, f"{temp_dir}//f.txt")

            assert path.read_text() == "hi"

    async def test_cross_location_file_transfer_trailing_slash(
        self, horus_context: HorusContext
    ) -> None:
        """A file transfers into a destination whose wd ends with ``/``."""
        del horus_context
        with (
            tempfile.TemporaryDirectory() as src_dir,
            tempfile.TemporaryDirectory() as dst_dir,
        ):
            src_file = Path(src_dir) / "f.txt"
            src_file.write_text("payload")

            source = _FarLocalTarget(working_directory=f"{src_dir}/")
            destination = _FarLocalTarget(working_directory=f"{dst_dir}/")
            artifact = FileArtifact(id="f", path=src_file)

            await GenericTransfer().transfer(artifact, source, destination)

            assert Path(dst_dir, "f.txt").read_text() == "payload"
            assert artifact.path == Path(dst_dir, "f.txt").resolve()

    async def test_failed_command_error_includes_stderr(self) -> None:
        """The RuntimeError carries the command's stderr, not just rc."""
        store = ArtifactStore(LocalTarget())
        artifact = FileArtifact(id="a", path=Path("/nonexistent/f.txt"))

        with pytest.raises(RuntimeError, match=r"exit code 3.*boom"):
            await store._run("echo boom >&2; exit 3", artifact, "unpackage")
