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

import tempfile
from pathlib import Path

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.target.local import LocalTarget
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.store import ArtifactStore, TargetFilesystem
from horus_runtime.core.transfer.generic import GenericTransfer


class _FarLocalTarget(LocalTarget):
    """
    A local target that pretends to live on a distinct machine.

    It reuses the real local filesystem primitives but reports a unique
    ``location_id`` (so transfers do not short-circuit) and SSH-like
    ``path_on_target`` semantics (``working_directory/<name>``), letting tests
    exercise the full package -> get_file -> put_file -> unpackage path against
    the local filesystem.
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
    LocalTarget must satisfy the TargetFilesystem protocol natively.
    """

    def test_local_target_satisfies_protocol(self) -> None:
        """LocalTarget structurally implements TargetFilesystem."""
        assert isinstance(LocalTarget(), TargetFilesystem)

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
