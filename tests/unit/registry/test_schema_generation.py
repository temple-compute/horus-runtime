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
Unit tests for AutoRegistry schema generation and dispatch behavior.
"""

from typing import ClassVar

import pydantic
import pytest
from pydantic import BaseModel, Field

from horus_runtime.registry.auto_registry import AutoRegistry


class SchemaRoot(AutoRegistry, entry_point="schema_test"):
    """
    Root class used across JSON schema and core schema tests.
    """

    registry_key: ClassVar[str] = "kind"
    add_to_registry: ClassVar[bool] = False

    kind: str
    required_field: str
    optional_field: str | None = None


class SchemaAlpha(SchemaRoot):
    """
    Concrete subclass of SchemaRoot.
    """

    kind: str = "alpha"
    alpha_specific: str = "alpha_value"
    add_to_registry: ClassVar[bool] = True


class SchemaBeta(SchemaRoot):
    """
    Another concrete subclass.
    """

    kind: str = "beta"
    beta_specific: int = 0
    add_to_registry: ClassVar[bool] = True


class _SchemaHost(BaseModel):
    """
    Wrapper model to trigger Pydantic dispatch through SchemaRoot.
    """

    item: SchemaRoot


@pytest.mark.unit
class TestAutoRegistryPydanticJsonSchema:
    """
    Tests for AutoRegistry.__get_pydantic_json_schema__.
    """

    def test_root_schema_type_is_object(self) -> None:
        """
        Root class schema must declare type: object.
        """
        schema = pydantic.TypeAdapter(SchemaRoot).json_schema()
        assert schema["type"] == "object"

    def test_root_schema_has_additional_properties(self) -> None:
        """
        Root class schema must allow additionalProperties for plugin fields.
        """
        schema = pydantic.TypeAdapter(SchemaRoot).json_schema()
        assert schema.get("additionalProperties") is True

    def test_root_schema_includes_root_fields_in_properties(self) -> None:
        """
        All root-class fields must appear under 'properties'.
        """
        schema = pydantic.TypeAdapter(SchemaRoot).json_schema()
        properties = schema.get("properties", {})
        assert "kind" in properties
        assert "required_field" in properties
        assert "optional_field" in properties

    def test_root_schema_required_fields_present(self) -> None:
        """
        Fields without defaults must appear in 'required'.
        """
        schema = pydantic.TypeAdapter(SchemaRoot).json_schema()
        required = schema.get("required", [])
        assert "required_field" in required

    def test_root_schema_optional_fields_not_required(self) -> None:
        """
        Fields with defaults must not appear in 'required'.
        """
        schema = pydantic.TypeAdapter(SchemaRoot).json_schema()
        required = schema.get("required", [])
        assert "optional_field" not in required

    def test_root_schema_excludes_concrete_subclass_fields(self) -> None:
        """
        Subclass-specific fields must not leak into the root schema.
        """
        schema = pydantic.TypeAdapter(SchemaRoot).json_schema()
        properties = schema.get("properties", {})
        assert "alpha_specific" not in properties
        assert "beta_specific" not in properties

    def test_concrete_subclass_uses_default_pydantic_schema(self) -> None:
        """
        Concrete subclasses must produce their own Pydantic schema,
        not the root override.
        """
        schema = pydantic.TypeAdapter(SchemaAlpha).json_schema()
        # Default Pydantic schema for a concrete model includes its own fields
        properties = schema.get("properties", {})
        assert "alpha_specific" in properties

    def test_root_without_required_fields_omits_required_key(self) -> None:
        """
        If a root class has no required fields the 'required' key must be
        absent.
        """

        class AllOptionalRoot(AutoRegistry, entry_point="all_optional_schema"):
            registry_key: ClassVar[str] = "kind"
            add_to_registry: ClassVar[bool] = False
            kind: str = "base"
            optional_a: str | None = None
            optional_b: int = 0

        schema = pydantic.TypeAdapter(AllOptionalRoot).json_schema()
        assert "required" not in schema


@pytest.mark.unit
class TestAutoRegistryPydanticCoreSchema:
    """
    Tests for AutoRegistry.__get_pydantic_core_schema__ dispatch logic.
    """

    def test_valid_dict_dispatches_to_correct_concrete_class(self) -> None:
        """
        A dict with a known discriminator value must deserialise to the right
        class.
        """
        result = _SchemaHost.model_validate(
            {"item": {"kind": "alpha", "required_field": "x"}}
        )
        assert isinstance(result.item, SchemaAlpha)

    def test_dispatch_sets_correct_field_values(self) -> None:
        """
        After dispatch, all fields including subclass-specific ones must be
        set.
        """
        result = _SchemaHost.model_validate(
            {
                "item": {
                    "kind": "beta",
                    "required_field": "y",
                    "beta_specific": 42,
                }
            }
        )
        assert isinstance(result.item, SchemaBeta)
        assert result.item.beta_specific == 42
        assert result.item.required_field == "y"

    def test_existing_instance_passes_through(self) -> None:
        """
        An already-instantiated subclass passed as field value must not be
        re-validated.
        """
        instance = SchemaAlpha(required_field="z")
        result = _SchemaHost.model_validate({"item": instance})
        assert result.item is instance

    def test_missing_discriminator_raises_value_error(self) -> None:
        """
        A dict without the discriminator key must raise ValueError.
        """
        with pytest.raises(pydantic.ValidationError) as exc_info:
            _SchemaHost.model_validate({"item": {"required_field": "x"}})
        assert "Missing" in str(exc_info.value) or "kind" in str(
            exc_info.value
        )

    def test_unknown_discriminator_value_raises_value_error(self) -> None:
        """
        A dict with an unregistered discriminator value must raise ValueError.
        """
        with pytest.raises(pydantic.ValidationError) as exc_info:
            _SchemaHost.model_validate(
                {"item": {"kind": "nonexistent", "required_field": "x"}}
            )
        assert "nonexistent" in str(exc_info.value)

    def test_non_dict_non_instance_raises_type_error(self) -> None:
        """
        Passing a non-dict, non-AutoRegistry value must raise TypeError.
        """
        with pytest.raises(TypeError) as exc_info:
            _SchemaHost.model_validate({"item": ["not", "a", "dict"]})
        assert "Expected dict" in str(exc_info.value) or "SchemaRoot" in str(
            exc_info.value
        )

    def test_concrete_subclass_validates_normally_without_dispatch(
        self,
    ) -> None:
        """
        A field typed as a concrete subclass must validate via Pydantic
        directly, not dispatch.
        """

        class DirectHost(BaseModel):
            item: SchemaAlpha

        result = DirectHost.model_validate(
            {"item": {"kind": "alpha", "required_field": "r"}}
        )
        assert isinstance(result.item, SchemaAlpha)
        assert result.item.required_field == "r"

    def test_dispatch_to_second_registered_class(self) -> None:
        """
        Dispatch must correctly route to SchemaBeta, not just the first
        registered class.
        """
        result = _SchemaHost.model_validate(
            {"item": {"kind": "beta", "required_field": "b"}}
        )
        assert isinstance(result.item, SchemaBeta)
        assert not isinstance(result.item, SchemaAlpha)

    def test_root_schema_with_nested_registry_root_field_does_not_crash(
        self,
    ) -> None:
        """
        A root class with a dict[str, AutoRegistryRoot] field must not raise
        PydanticInvalidForJsonSchema during parent-context schema generation.
        """

        class InnerRoot(AutoRegistry, entry_point="inner_nested"):
            registry_key: ClassVar[str] = "kind"
            add_to_registry: ClassVar[bool] = False
            kind: str

        class OuterRoot(AutoRegistry, entry_point="outer_nested"):
            registry_key: ClassVar[str] = "kind"
            add_to_registry: ClassVar[bool] = False
            kind: str
            items: dict[str, InnerRoot]

        class Host(BaseModel):
            payload: OuterRoot

        # Must not raise PydanticInvalidForJsonSchema
        schema = Host.model_json_schema()
        assert "payload" in schema["properties"]

    def test_root_schema_preserves_field_constraints(self) -> None:
        """
        Field constraints from Field(...) must appear in the emitted JSON
        schema.
        """

        class ConstrainedRoot(AutoRegistry, entry_point="constrained_schema"):
            registry_key: ClassVar[str] = "kind"
            add_to_registry: ClassVar[bool] = False
            kind: str
            name: str = Field(
                min_length=1, max_length=64, pattern=r"^[a-z_]+$"
            )
            count: int = Field(ge=0, le=100)

        schema = pydantic.TypeAdapter(ConstrainedRoot).json_schema()
        properties = schema["properties"]

        assert properties["name"].get("minLength") == 1
        assert properties["name"].get("maxLength") == 64
        assert properties["name"].get("pattern") == r"^[a-z_]+$"
        assert properties["count"].get("minimum") == 0
        assert properties["count"].get("maximum") == 100

    def test_root_schema_preserves_max_length_constraint(self) -> None:
        """
        Field(max_length=N) must produce 'maxLength: N' in the emitted JSON
        schema so that OpenAPI clients enforce the upper bound.
        """

        class MaxLengthRoot(AutoRegistry, entry_point="max_length_schema"):
            registry_key: ClassVar[str] = "kind"
            add_to_registry: ClassVar[bool] = False
            kind: str
            tag: str = Field(max_length=10)

        schema = pydantic.TypeAdapter(MaxLengthRoot).json_schema()
        assert schema["properties"]["tag"].get("maxLength") == 10
