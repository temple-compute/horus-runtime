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

import hashlib
import tempfile
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact

SHA_HEX_LENGTH = 64  # Length of SHA-256 hash in hexadecimal representation
SHA_HEX_LENGTH_BYTES = 32  # Length of SHA-256 hash in bytes


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

    def test_hash_property_existing_file(self) -> None:
        """
        Test hash property with an existing file.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.txt"
            test_content = "Hello, Horus Runtime!"
            test_file.write_text(test_content)

            artifact = FileArtifact(id="test_file", path=test_file)

            # Calculate expected hash
            expected_hash = hashlib.sha256(test_content.encode()).hexdigest()

            assert artifact.hash == expected_hash
            assert isinstance(artifact.hash, str)

    def test_hash_property_nonexistent_file(self) -> None:
        """
        Test hash property returns None for non-existent file.
        """
        artifact = FileArtifact(
            id="nonexistent_file", path=Path("/nonexistent/file.txt")
        )
        assert artifact.hash is None

    def test_hash_property_empty_file(self) -> None:
        """
        Test hash property with empty file.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_file = Path(temp_dir) / "empty.txt"
            empty_file.write_text("")  # Create empty file

            artifact = FileArtifact(id="empty_file", path=empty_file)

            # Hash of empty content
            expected_hash = hashlib.sha256(b"").hexdigest()
            assert artifact.hash == expected_hash

    def test_hash_changes_with_content(self) -> None:
        """
        Test that hash changes when file content changes.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "changeable.txt"

            # Initial content
            test_file.write_text("Initial content")
            artifact = FileArtifact(id="changeable_file", path=test_file)
            hash1 = artifact.hash

            # Change content
            test_file.write_text("Modified content")
            hash2 = artifact.hash

            assert hash1 != hash2
            assert hash1 is not None
            assert hash2 is not None

    def test_large_file_hash_performance(self) -> None:
        """
        Test hash calculation with larger file (tests chunked reading).
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            large_file = Path(temp_dir) / "large.txt"

            # Create a file larger than the hash buffer (65536 bytes)
            large_content = "A" * 100000  # 100KB
            large_file.write_text(large_content)

            artifact = FileArtifact(id="large_file", path=large_file)

            # Should complete without error
            file_hash = artifact.hash
            assert file_hash is not None
            assert isinstance(file_hash, str)
            assert len(file_hash) == SHA_HEX_LENGTH

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

    def test_package_returns_canonical_file_path(
        self, horus_context: HorusContext
    ) -> None:
        """
        Test that packaging a file artifact returns its canonical path.
        """
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.txt"
            test_file.write_text("hello")

            artifact = FileArtifact(id="test_file", path=test_file)

            assert artifact.package() == test_file.resolve()

    def test_unpackage_copies_packaged_file_to_artifact_path(
        self, horus_context: HorusContext
    ) -> None:
        """
        Test that unpackage copies the packaged file into place.
        """
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = Path(temp_dir) / "package.txt"
            target_path = Path(temp_dir) / "output" / "artifact.txt"
            package_path.write_text("hello")

            artifact = FileArtifact(id="artifact_file", path=target_path)
            artifact.unpackage(package_path)

            assert target_path.read_text() == "hello"


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

    def test_exists_method_existing_directory(self) -> None:
        """
        Test exists method with existing directory.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = FolderArtifact(id="test_folder", path=Path(temp_dir))
            assert artifact.exists() is True

    def test_exists_method_nonexistent_directory(self) -> None:
        """
        Test exists method with non-existent directory.
        """
        artifact = FolderArtifact(
            id="nonexistent_folder", path=Path("/nonexistent/directory")
        )
        assert artifact.exists() is False

    def test_exists_method_file_not_directory(self) -> None:
        """
        Test exists method returns False when path exists but is a file.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "not_a_folder.txt"
            test_file.write_text("I am a file")

            artifact = FolderArtifact(id="not_a_folder", path=test_file)
            assert (
                artifact.exists() is False
            )  # File exists but it's not a directory

    def test_hash_property_empty_folder(self) -> None:
        """
        Test hash property with empty folder.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = FolderArtifact(id="empty_folder", path=Path(temp_dir))

            folder_hash = artifact.hash

            # Empty folder should have deterministic hash
            assert folder_hash is not None
            assert isinstance(folder_hash, str)
            assert len(folder_hash) == SHA_HEX_LENGTH

    def test_hash_property_nonexistent_folder(self) -> None:
        """
        Test hash property returns None for non-existent folder.
        """
        artifact = FolderArtifact(
            id="nonexistent_folder", path=Path("/nonexistent/folder")
        )
        assert artifact.hash is None

    def test_hash_property_multiple_files(self) -> None:
        """
        Test hash property with folder containing multiple files.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create multiple files in specific order
            files_data = [
                ("a_first.txt", "Content A"),
                ("b_second.txt", "Content B"),
                ("z_last.txt", "Content Z"),
            ]

            for filename, content in files_data:
                file_path = Path(temp_dir) / filename
                file_path.write_text(content)

            artifact = FolderArtifact(id="test_folder", path=Path(temp_dir))
            folder_hash = artifact.hash

            assert folder_hash is not None
            assert isinstance(folder_hash, str)

            # Verify deterministic ordering (alphabetical by relative path)
            # Hash should be consistent regardless of file creation order
            artifact2 = FolderArtifact(id="test_folder", path=Path(temp_dir))
            assert artifact2.hash == folder_hash

    def test_hash_empty_folders_ignored(self) -> None:
        """
        Test that empty subfolders don't affect hash (only files are hashed).
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create folder with empty subdirectory
            empty_subdir = Path(temp_dir) / "empty"
            empty_subdir.mkdir()

            artifact = FolderArtifact(id="test_folder", path=Path(temp_dir))
            hash_with_empty_dir = artifact.hash

            # Remove empty directory
            empty_subdir.rmdir()

            hash_without_empty_dir = artifact.hash

            # Hash should be the same (empty directories don't
            # contribute to hash)
            assert hash_with_empty_dir == hash_without_empty_dir

    def test_folder_with_special_characters(self) -> None:
        """
        Test folder hash with files containing special characters.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create files with Unicode and special characters in names/content
            special_file = Path(temp_dir) / "special_字符.txt"
            special_file.write_text("Content with émojis 🚀 and ñoño")

            artifact = FolderArtifact(id="special_folder", path=Path(temp_dir))
            folder_hash = artifact.hash

            assert folder_hash is not None
            assert isinstance(folder_hash, str)

    def test_large_folder_structure(self) -> None:
        """
        Test hash calculation with larger folder structure.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create multiple levels and files
            for i in range(10):
                subdir = Path(temp_dir) / f"subdir_{i:02d}"
                subdir.mkdir()
                for j in range(5):
                    file_path = subdir / f"file_{j}.txt"
                    file_path.write_text(f"Content {i}-{j}")

            artifact = FolderArtifact(id="large_folder", path=Path(temp_dir))
            folder_hash = artifact.hash

            # Should complete without error even with many files
            assert folder_hash is not None
            assert isinstance(folder_hash, str)
            assert len(folder_hash) == SHA_HEX_LENGTH

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

    def test_package_creates_zip_archive(
        self, horus_context: HorusContext
    ) -> None:
        """
        Test that packaging a folder creates a zip archive.
        """
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            folder_path = Path(temp_dir) / "folder"
            folder_path.mkdir()
            (folder_path / "a.txt").write_text("A")

            artifact = FolderArtifact(id="test_folder", path=folder_path)
            package_path = artifact.package()

            assert package_path.suffix == ".zip"
            assert package_path.exists()

    def test_unpackage_extracts_archive_to_folder(
        self, horus_context: HorusContext
    ) -> None:
        """
        Test that unpackage extracts a packaged folder archive.
        """
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source"
            target_path = Path(temp_dir) / "target"
            source_path.mkdir()
            (source_path / "a.txt").write_text("A")
            (source_path / "nested").mkdir()
            (source_path / "nested" / "b.txt").write_text("B")

            source_artifact = FolderArtifact(
                id="source_folder", path=source_path
            )
            package_path = source_artifact.package()

            target_artifact = FolderArtifact(
                id="target_folder", path=target_path
            )
            target_artifact.unpackage(package_path)

            assert (target_path / "a.txt").read_text() == "A"
            assert (target_path / "nested" / "b.txt").read_text() == "B"


class ConcreteLocalArtifact(BaseArtifact):
    """
    Concrete implementation of BaseArtifact for testing.
    """

    add_to_registry = False
    kind: str = "local_test"

    def exists(self) -> bool:
        """
        Test exists implementation based on the local path.
        """
        return self.path.exists()

    @property
    def hash(self) -> str | None:
        """
        Test hash property.
        """
        return "test_hash" if self.exists() else None

    def delete(self) -> None:
        """
        Test delete method.
        """
        pass

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

    def test_exists_method_file_exists(self) -> None:
        """
        Test exists method when file exists.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "existing_file.txt"
            test_file.write_text("content")

            artifact = ConcreteLocalArtifact(
                id="existing_file_artifact", path=test_file
            )
            assert artifact.exists() is True

    def test_exists_method_file_not_exists(self) -> None:
        """
        Test exists method when file does not exist.
        """
        artifact = ConcreteLocalArtifact(
            id="nonexistent_file_artifact",
            path=Path("/nonexistent/path/file.txt"),
        )
        assert artifact.exists() is False

    def test_hash_file_static_method(self) -> None:
        """
        Test the static hash_file method.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test.txt"
            test_content = "Hello, Horus!"
            test_file.write_text(test_content)

            hash_bytes = BaseArtifact.hash_file(test_file)

            assert isinstance(hash_bytes, bytes)
            assert len(hash_bytes) == SHA_HEX_LENGTH_BYTES

            # Hash should be consistent
            hash_bytes2 = BaseArtifact.hash_file(test_file)
            assert hash_bytes == hash_bytes2

    def test_hash_file_different_contents(self) -> None:
        """
        Test that different file contents produce different hashes.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            file1 = Path(temp_dir) / "file1.txt"
            file2 = Path(temp_dir) / "file2.txt"

            file1.write_text("Content A")
            file2.write_text("Content B")

            hash1 = BaseArtifact.hash_file(file1)
            hash2 = BaseArtifact.hash_file(file2)

            assert hash1 != hash2

    def test_hash_file_large_file(self) -> None:
        """
        Test hash_file method with large file to verify chunked reading.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            large_file = Path(temp_dir) / "large.txt"

            # Create a file larger than the 64KB buffer
            large_content = "A" * (65536 * 2)  # 128KB
            large_file.write_text(large_content)

            hash_bytes = BaseArtifact.hash_file(large_file)

            assert isinstance(hash_bytes, bytes)
            assert len(hash_bytes) == SHA_HEX_LENGTH_BYTES

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
