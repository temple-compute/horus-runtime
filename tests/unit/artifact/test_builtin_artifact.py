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
Unit tests for artifact_registry module.
"""

import tempfile
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact


@pytest.mark.unit
class TestInitRegistry:
    """
    Test that the builtin horus artifacts are properly registered.
    """

    def test_init_registry_scans_builtin_artifacts(
        self,
    ) -> None:
        """
        Test that init_registry scans the core artifacts package.
        """
        # Should have scanned the core artifacts package
        assert "file" in BaseArtifact.registry
        assert "folder" in BaseArtifact.registry

        assert BaseArtifact.registry["file"] is FileArtifact
        assert BaseArtifact.registry["folder"] is FolderArtifact


@pytest.mark.unit
class TestArtifactRegistry:
    """
    Test cases for ArtifactUnion type alias.
    """

    def test_artifact_union_can_validate_union_artifact(self) -> None:
        """
        Test that ArtifactUnion can validate FileArtifact data.
        """
        data = [
            {"path": "/test/path.txt", "kind": "file", "id": "file_artifact"},
            {
                "path": "/test/folder",
                "kind": "folder",
                "id": "folder_artifact",
            },
        ]

        class TestModel(BaseModel):
            artifact: list[BaseArtifact]

        # This should work with the discriminated union
        result = TestModel.model_validate({"artifact": data})

        # Check FileArtifact
        assert isinstance(result.artifact[0], FileArtifact)
        assert result.artifact[0].kind == "file"

        # Check FolderArtifact
        assert isinstance(result.artifact[1], FolderArtifact)
        assert result.artifact[1].kind == "folder"

    def test_artifact_registry_invalid_kind_handling(self) -> None:
        """
        Test handling of invalid kind values.
        """
        invalid_data = [{"path": "/test/path.txt", "kind": "invalid_type"}]

        class TestModel(BaseModel):
            artifact: list[BaseArtifact]

        # Should raise validation error for unknown kind
        with pytest.raises(ValidationError):
            # Try to validate with a known artifact type - should fail
            # because kind doesn't match
            TestModel.model_validate({"artifact": invalid_data})


@pytest.mark.integration
class TestArtifactRegistryIntegration:
    """
    Integration tests for the full artifact registry system.
    """

    def test_registry_contains_expected_artifacts(self) -> None:
        """
        Test that the registry contains the expected artifact types.
        """
        # Registry should contain file and folder artifacts
        assert hasattr(BaseArtifact, "registry")
        assert "file" in BaseArtifact.registry
        assert "folder" in BaseArtifact.registry


@pytest.mark.unit
class TestFileArtifact:
    """
    Test cases for FileArtifact class.
    """

    def test_file_artifact_instantiation(self) -> None:
        """
        Test FileArtifact can be instantiated.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.txt"
            test_file.write_text("content")

            artifact = FileArtifact(id="test_file", path=test_file)

            assert artifact is not None
            assert artifact.kind == "file"
            assert artifact.path == test_file.resolve()

    def test_read_returns_file_contents(
        self, horus_context: HorusContext
    ) -> None:
        """
        Test that read returns the full text contents of the file.
        """
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.txt"
            test_file.write_text("hello")

            artifact = FileArtifact(id="test_file", path=test_file)

            assert artifact.read() == "hello"

    def test_write_materializes_file_contents(
        self, horus_context: HorusContext
    ) -> None:
        """
        Test that write materializes text content at the artifact path.
        """
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "nested" / "test.txt"
            artifact = FileArtifact(id="test_file", path=test_file)

            artifact.write("hello")

            assert test_file.read_text() == "hello"

    def test_file_pack_unpack_commands_are_identity(self) -> None:
        """
        A single-file artifact is its own package: both command builders
        return None so the store handles it by identity.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.txt"
            test_file.write_text("hello")

            artifact = FileArtifact(id="test_file", path=test_file)

            assert artifact.pack_command(str(test_file), "/tmp/pkg") is None
            assert artifact.unpack_command("/tmp/pkg", str(test_file)) is None


@pytest.mark.unit
class TestFolderArtifact:
    """
    Test cases for FolderArtifact class.
    """

    def test_folder_artifact_instantiation(self) -> None:
        """
        Test FolderArtifact can be instantiated.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = FolderArtifact(id="test_folder", path=Path(temp_dir))

            assert artifact is not None
            assert artifact.kind == "folder"
            assert artifact.path == Path(temp_dir).resolve()

    def test_read_returns_folder_path(
        self, horus_context: HorusContext
    ) -> None:
        """
        Test that read returns the canonical folder path.
        """
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            folder_path = Path(temp_dir)
            artifact = FolderArtifact(id="test_folder", path=folder_path)

            assert artifact.read() == folder_path.resolve()

    def test_write_copies_directory_contents(
        self, horus_context: HorusContext
    ) -> None:
        """
        Test that write materializes the folder from a source directory.
        """
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source"
            target_path = Path(temp_dir) / "target"
            source_path.mkdir()
            (source_path / "a.txt").write_text("A")
            (source_path / "nested").mkdir()
            (source_path / "nested" / "b.txt").write_text("B")

            artifact = FolderArtifact(id="test_folder", path=target_path)
            artifact.write(source_path)

            assert (target_path / "a.txt").read_text() == "A"
            assert (target_path / "nested" / "b.txt").read_text() == "B"

    def test_pack_command_tars_folder_contents(self) -> None:
        """
        A folder's pack command tars the directory's *contents* (via
        ``tar -C src .``) into the package path.
        """
        artifact = FolderArtifact(id="f", path=Path("/work/data"))

        cmd = artifact.pack_command("/work/data", "/tmp/data.horuspkg")

        assert cmd is not None
        assert "tar czf" in cmd
        assert "-C /work/data ." in cmd
        assert "/tmp/data.horuspkg" in cmd

    def test_unpack_command_extracts_into_fresh_dest(self) -> None:
        """
        A folder's unpack command recreates the destination and extracts the
        tarball into it.
        """
        artifact = FolderArtifact(id="f", path=Path("/work/data"))

        cmd = artifact.unpack_command("/tmp/data.horuspkg", "/dest/data")

        assert cmd is not None
        assert "rm -rf /dest/data" in cmd
        assert "mkdir -p /dest/data" in cmd
        assert "tar xzf /tmp/data.horuspkg -C /dest/data" in cmd


class ConcreteLocalArtifact(BaseArtifact):
    """
    Concrete implementation of BaseArtifact for testing.
    """

    add_to_registry = False
    kind: str = "local_test"

    def read(self) -> Path:
        """
        Test read method.
        """
        return self.path

    def write(self, value: Path) -> None:
        """
        Test write method.
        """
        self.path = Path(value)


@pytest.mark.unit
class TestBaseArtifactPathBehavior:
    """
    Test cases for path-backed behavior on BaseArtifact subclasses.
    """

    def test_local_artifact_instantiation_with_path(self) -> None:
        """
        Test creating local artifact with a Path.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.txt"
            test_file.write_text("test content")

            artifact = ConcreteLocalArtifact(
                id="test_artifact", path=test_file
            )

        assert artifact.path == test_file.resolve()

    def test_local_artifact_instantiation_with_path_string(self) -> None:
        """
        Test creating local artifact with path string.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.txt"
            test_file.write_text("test content")

            artifact = ConcreteLocalArtifact(
                id="test_artifact", path=test_file
            )

            assert artifact.path == test_file.resolve()

    def test_path_update_on_model_validation(self) -> None:
        """
        Test that path is resolved to an absolute path after validation.
        """
        # Create with relative path
        artifact = ConcreteLocalArtifact(
            id="test_artifact", path=Path("./test.txt")
        )

        # Path should be updated to an absolute path
        assert artifact.path.is_absolute()
