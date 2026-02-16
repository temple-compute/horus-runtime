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
Unit tests for BaseArtifact class
"""

import uuid
from abc import ABC
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.registry.auto_registry import (
    AutoRegistry,
    RegistryKeyIsNoneError,
)


class ConcreteTestArtifact(BaseArtifact):
    """
    Concrete implementation of BaseArtifact for testing purposes.
    """

    add_to_registry = False  # Prevent registry pollution in tests
    kind: Literal["test"] = "test"

    def exists(self) -> bool:
        return True

    def materialize(self) -> Path:
        return Path("/tmp/test")

    @property
    def hash(self) -> str | None:
        return "test_hash"


@pytest.mark.unit
class TestBaseArtifact:
    """
    Test cases for BaseArtifact abstract base class
    """

    def test_base_artifact_is_abstract(self) -> None:
        """
        Test that BaseArtifact cannot be instantiated directly
        """
        with pytest.raises(
            TypeError, match="Can't instantiate abstract class"
        ):

            # We use type:ignore because the linter correctly identifies that
            # BaseArtifact is abstract and cannot be instantiated, but we want
            # to test this behavior explicitly at runtime.
            BaseArtifact(uri="test://uri")  # type: ignore

    def test_base_artifact_inherits_correctly(self) -> None:
        """
        Test that BaseArtifact inherits from expected classes
        """

        # This is a little bit redundant since we know BaseArtifact is defined
        # as inheriting from these, but it serves as a sanity check that the
        # class hierarchy is correct. also can signal for future refactors if
        # the inheritance changes.
        assert issubclass(BaseArtifact, BaseModel)
        assert issubclass(BaseArtifact, ABC)
        assert issubclass(BaseArtifact, AutoRegistry)

    def test_registry_key_is_kind(self) -> None:
        """
        Test that BaseArtifact uses 'kind' as registry key
        """

        # This check will be done for other classes that inherit from
        # autoregistry. For artifact, the registry key is 'kind',
        # so we want to make sure that this is set correctly in the base class.
        assert BaseArtifact.registry_key == "kind"

    def test_uuid_auto_generation(self) -> None:
        """
        Test that UUID is automatically generated if not provided
        """
        artifact1 = ConcreteTestArtifact(uri="test://uri1")
        artifact2 = ConcreteTestArtifact(uri="test://uri2")

        assert artifact1.id != artifact2.id
        assert isinstance(artifact1.id, uuid.UUID)
        assert isinstance(artifact2.id, uuid.UUID)

    def test_custom_uuid_accepted(self) -> None:
        """
        Test that custom UUID is accepted when provided
        """
        custom_id = uuid.uuid4()
        artifact = ConcreteTestArtifact(uri="test://uri", id=custom_id)

        assert artifact.id == custom_id

    def test_uri_field_required(self) -> None:
        """
        Test that uri field is required
        """
        with pytest.raises(ValidationError) as exc_info:
            # This should fail because uri is a required field and we're
            # not providing it. We use type:ignore because the linter will
            # complain about missing required fields, but we want to test this
            # validation at runtime.
            ConcreteTestArtifact()  # type: ignore

        # Check that the validation error is for the 'uri' field
        errors = exc_info.value.errors()
        assert any(error["loc"] == ("uri",) for error in errors)

    def test_kind_validation_in_subclass(self) -> None:
        """
        Test that kind field validation works in subclasses
        """
        # This should work since ConcreteTestArtifact sets kind = "test"
        artifact = ConcreteTestArtifact(uri="test://uri")
        assert artifact.kind == "test"

    def test_abstract_methods_defined(self) -> None:
        """
        Test that abstract methods are properly defined in concrete class
        """
        artifact = ConcreteTestArtifact(uri="test://uri")

        # Test exists method
        assert artifact.exists() is True

        # Test materialize method
        path = artifact.materialize()
        assert isinstance(path, Path)
        assert path == Path("/tmp/test")

        # Test hash property
        assert artifact.hash == "test_hash"

    def test_artifact_serialization(self) -> None:
        """
        Test that artifacts can be serialized to dict
        """
        artifact = ConcreteTestArtifact(uri="test://uri")
        artifact_dict = artifact.model_dump()

        assert "id" in artifact_dict
        assert "uri" in artifact_dict
        assert "kind" in artifact_dict
        assert artifact_dict["uri"] == "test://uri"
        assert artifact_dict["kind"] == "test"

    def test_artifact_deserialization(self) -> None:
        """
        Test that artifacts can be deserialized from dict
        """
        test_id = uuid.uuid4()
        data = {"id": str(test_id), "uri": "test://uri", "kind": "test"}

        artifact = ConcreteTestArtifact.model_validate(data)

        assert artifact.id == test_id
        assert artifact.uri == "test://uri"
        assert artifact.kind == "test"


@pytest.mark.unit
class TestBaseArtifactValidation:
    """
    Test validation behavior of BaseArtifact
    """

    def test_kind_field_must_be_set_in_subclass(self) -> None:
        """
        Test that subclasses must set the kind field
        """
        # This artifact doesn't set kind, so validation should fail
        with pytest.raises(
            RegistryKeyIsNoneError,
            match="must define a class property named 'kind'",
        ):

            class InvalidArtifactNoKind(  # pyright: ignore[reportUnusedClass]
                BaseArtifact
            ):
                """
                Invalid artifact implementation without kind field for testing
                """

                add_to_registry = True

                def exists(self) -> bool:
                    return False

                def materialize(self) -> Path:
                    return Path("/tmp")

                @property
                def hash(self) -> str | None:
                    return None

    def test_model_validation_preserves_type_safety(self) -> None:
        """
        Test that Pydantic validation maintains type safety
        """
        with pytest.raises(ValidationError):
            # We use type:ignore here because we're intentionally passing
            # the wrong type # for the 'id' field. The linter # will complain
            # about this, but we want to ensure that the validation error
            # is raised at runtime.
            ConcreteTestArtifact(
                uri="test://uri", id="not-a-uuid"  # type: ignore
            )

    def test_extra_fields_handling(self) -> None:
        """
        Test behavior with extra fields in model validation
        """
        data = {
            "uri": "test://uri",
            "kind": "test",
            "extra_field": "should_be_ignored",
        }

        # Should work fine - extra fields are ignored by default
        artifact = ConcreteTestArtifact.model_validate(data)
        assert artifact.uri == "test://uri"
        assert artifact.kind == "test"
