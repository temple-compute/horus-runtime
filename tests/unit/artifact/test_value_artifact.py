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
Unit tests for the value-carrying artifact kinds (number, boolean, string).
"""

import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.artifact.boolean import BooleanArtifact
from horus_builtin.artifact.number import NumberArtifact
from horus_builtin.artifact.string import StringArtifact
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact


@pytest.mark.unit
class TestValueArtifactRegistry:
    """
    The value kinds register under the artifact registry like any other kind.
    """

    def test_new_kinds_are_registered(self) -> None:
        """All three value kinds resolve through the AutoRegistry."""
        assert "number" in BaseArtifact.registry
        assert "boolean" in BaseArtifact.registry
        assert "string" in BaseArtifact.registry
        assert BaseArtifact.registry["number"] is NumberArtifact
        assert BaseArtifact.registry["boolean"] is BooleanArtifact
        assert BaseArtifact.registry["string"] is StringArtifact
        assert "multichoice" in BaseArtifact.registry
        assert "list" in BaseArtifact.registry

    def test_discriminated_union_round_trips_values(self) -> None:
        """Values survive model_dump/model_validate round trips by kind."""

        class TestModel(BaseModel):
            artifact: list[BaseArtifact]

        result = TestModel.model_validate(
            {
                "artifact": [
                    {"id": "n", "kind": "number", "value": 10, "path": "/a"},
                    {
                        "id": "b",
                        "kind": "boolean",
                        "value": True,
                        "path": "/b",
                    },
                    {"id": "s", "kind": "string", "value": "hi", "path": "/c"},
                ]
            }
        )
        values = [
            cast(NumberArtifact, result.artifact[0]).value,
            cast(BooleanArtifact, result.artifact[1]).value,
            cast(StringArtifact, result.artifact[2]).value,
        ]
        assert values == [10, True, "hi"]


@pytest.mark.unit
class TestNumberArtifact:
    """NumberArtifact serializes an int/float value as a JSON number."""

    def test_defaults(self) -> None:
        """A bare number artifact defaults to 0 with kind ``number``."""
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = NumberArtifact(id="n", path=Path(temp_dir) / "n.json")
            assert artifact.kind == "number"
            assert artifact.value == 0

    def test_write_then_read_round_trips(
        self, horus_context: HorusContext
    ) -> None:
        """write() persists an int and read() returns it."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "n.json"
            artifact = NumberArtifact(id="n", path=target)
            artifact.write(10)
            assert target.read_text() == "10"
            assert artifact.read() == 10

    def test_float_is_preserved(self, horus_context: HorusContext) -> None:
        """A float value survives the write/read round trip."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = NumberArtifact(id="n", path=Path(temp_dir) / "n.json")
            artifact.write(3.5)
            assert artifact.read() == 3.5


@pytest.mark.unit
class TestBooleanArtifact:
    """BooleanArtifact serializes a bool as lowercase JSON true/false."""

    def test_defaults(self) -> None:
        """A bare boolean artifact defaults to False."""
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = BooleanArtifact(id="b", path=Path(temp_dir) / "b.json")
            assert artifact.kind == "boolean"
            assert artifact.value is False

    def test_write_then_read_round_trips(
        self, horus_context: HorusContext
    ) -> None:
        """write(True) persists ``true`` and read() returns True."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "b.json"
            artifact = BooleanArtifact(id="b", path=target)
            artifact.write(True)
            assert target.read_text() == "true"
            assert artifact.read() is True


@pytest.mark.unit
class TestStringArtifact:
    """StringArtifact serializes text as a plain UTF-8 file."""

    def test_defaults(self) -> None:
        """A bare string artifact defaults to an empty string."""
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = StringArtifact(id="s", path=Path(temp_dir) / "s.txt")
            assert artifact.kind == "string"
            assert artifact.value == ""

    def test_write_then_read_round_trips(
        self, horus_context: HorusContext
    ) -> None:
        """write() persists text and read() returns it."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "s.txt"
            artifact = StringArtifact(id="s", path=target)
            artifact.write("hello")
            assert target.read_text() == "hello"
            assert artifact.read() == "hello"


@pytest.mark.unit
class TestValueArtifactMaterialize:
    """Value artifacts materialize their authored value to a backing file."""

    def test_materialize_writes_value_when_file_missing(
        self, horus_context: HorusContext
    ) -> None:
        """A missing backing file is written from the authored value."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "n.json"
            artifact = NumberArtifact(id="n", path=target, value=42)
            assert not target.exists()
            artifact.materialize()
            assert target.read_text() == "42"

    def test_materialize_rewrites_a_stale_file(
        self, horus_context: HorusContext
    ) -> None:
        """A file left with another value is rewritten from the value."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "s.txt"
            target.write_text("old value")
            artifact = StringArtifact(id="s", path=target, value="edited")
            artifact.materialize()
            assert target.read_text() == "edited"

    def test_materialize_rewrites_a_corrupt_file(
        self, horus_context: HorusContext
    ) -> None:
        """A file that does not parse as the kind is rewritten."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "n.json"
            target.write_text("not json")
            artifact = NumberArtifact(id="n", path=target, value=7)
            artifact.materialize()
            assert target.read_text() == "7"

    def test_materialize_leaves_an_up_to_date_file_alone(
        self, horus_context: HorusContext
    ) -> None:
        """A file that already holds the value is not written again."""
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "n.json"
            artifact = NumberArtifact(id="n", path=target, value=42)
            artifact.materialize()
            with patch.object(NumberArtifact, "write") as write:
                artifact.materialize()
            write.assert_not_called()

    def test_base_artifact_materialize_is_noop(self) -> None:
        """BaseArtifact.materialize() is a no-op for non-value artifacts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "f.txt"
            artifact = BaseArtifact.model_validate(
                {"id": "f", "kind": "file", "path": str(target)}
            )
            artifact.materialize()
            assert not target.exists()


GPU = {"value": "gpu", "label": "GPU"}
CPU = {"value": "cpu", "label": None}


def _mapped(**kwargs: object) -> BooleanArtifact:
    return BooleanArtifact.model_validate(
        {"id": "device", "path": "/d.json", "on": GPU, "off": CPU, **kwargs}
    )


@pytest.mark.unit
class TestBooleanMapping:
    """``on``/``off`` map a boolean's two states to the values a task gets."""

    def test_plain_boolean_is_unchanged(self) -> None:
        """No mapping: value stays a bool, coerced like ``value: bool``."""
        artifact = BooleanArtifact.model_validate(
            {"id": "b", "kind": "boolean", "path": "/b", "value": "true"}
        )
        assert artifact.value is True
        assert artifact.on is None and artifact.off is None
        with pytest.raises(ValueError, match="valid boolean"):
            BooleanArtifact(id="b", path=Path("/b"), value="gpu")

    def test_default_state_maps_to_off(self) -> None:
        """An unset value is the off state, i.e. the off value."""
        assert _mapped().value == "cpu"

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(True, "gpu"), (False, "cpu"), ("gpu", "gpu"), ("cpu", "cpu")],
    )
    def test_value_holds_the_mapped_value(
        self, given: object, expected: str
    ) -> None:
        """A state is translated; a mapped value is kept as-is."""
        assert _mapped(value=given).value == expected

    def test_dump_reload_round_trips(self) -> None:
        """The document keeps the mapping and the mapped value."""
        artifact = _mapped(value=True)
        dumped = artifact.model_dump(mode="json")
        assert dumped["value"] == "gpu"
        assert dumped["on"] == GPU
        reloaded = BaseArtifact.model_validate(dumped)
        assert isinstance(reloaded, BooleanArtifact)
        assert reloaded.value == "gpu"
        assert reloaded.on == artifact.on

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"value": "tpu"}, "neither the 'on' value"),
            ({"off": GPU}, "different values"),
            ({"off": None}, "both 'on' and 'off'"),
            ({"on": {"value": True}}, "cannot be true/false"),
        ],
    )
    def test_invalid_mappings_are_rejected(
        self, kwargs: dict[str, object], match: str
    ) -> None:
        """Half a mapping, duplicates, bool values and strays all fail."""
        with pytest.raises(ValueError, match=match):
            _mapped(**kwargs)

    def test_write_and_read_use_the_mapped_value(
        self, horus_context: HorusContext
    ) -> None:
        """
        A producer may write the state or the value; the file and read()
        always carry the value the task receives.
        """
        del horus_context
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "d.json"
            artifact = _mapped(path=str(target))
            artifact.write(True)
            assert target.read_text() == '"gpu"'
            assert artifact.read() == "gpu"
            # A producer that wrote the bare state still reads as the value.
            target.write_text("false")
            assert artifact.read() == "cpu"
            # A plain boolean keeps writing JSON true/false.
            plain = BooleanArtifact(id="b", path=Path(temp_dir) / "b.json")
            plain.write(False)
            assert plain.path.read_text() == "false"
            assert plain.read() is False


@pytest.mark.unit
class TestNumberBounds:
    """``min``/``max``/``step`` bound the value; the grid starts at ``min``."""

    @pytest.mark.parametrize(
        "fields",
        [
            {"min": 0, "max": 10, "value": 10},
            {"min": 0, "step": 0.1, "value": 0.3},
            {"min": 1, "step": 2, "value": 5},
            {"step": 0.25, "value": -1.5},
        ],
    )
    def test_values_in_range_and_on_the_grid_pass(
        self, fields: dict[str, object]
    ) -> None:
        """Bounds are inclusive; float steps tolerate rounding."""
        NumberArtifact.model_validate({"id": "n", "path": "/n", **fields})

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"min": 5, "max": 1}, "is above max"),
            ({"min": 0, "max": 10, "value": 11}, "outside"),
            ({"min": 5, "value": 0}, "outside"),
            ({"min": 1, "step": 2, "value": 4}, "not a multiple of step"),
            ({"step": 0}, "greater than 0"),
        ],
    )
    def test_invalid_bounds_are_rejected(
        self, fields: dict[str, object], message: str
    ) -> None:
        """Inverted, out-of-range and off-grid values fail clearly."""
        with pytest.raises(ValidationError, match=message):
            NumberArtifact.model_validate({"id": "n", "path": "/n", **fields})

    def test_unset_value_starts_at_min(self) -> None:
        """A stripped value is valid again instead of failing on 0."""
        artifact = NumberArtifact.model_validate(
            {"id": "n", "path": "/n", "min": 5, "max": 10}
        )
        assert artifact.value == 5


@pytest.mark.unit
class TestStringConstraints:
    """``pattern`` must match the whole value; ``max_length`` caps it."""

    def test_full_match_passes(self) -> None:
        """A value matching the whole pattern is valid."""
        artifact = StringArtifact.model_validate(
            {"id": "s", "path": "/s", "pattern": r"\d+", "value": "42"}
        )
        assert artifact.multiline is False

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"pattern": r"\d+", "value": "42a"}, "does not match"),
            ({"pattern": "(", "value": ""}, "invalid pattern"),
            ({"max_length": 2, "value": "abc"}, "at most 2 characters"),
        ],
    )
    def test_invalid_values_are_rejected(
        self, fields: dict[str, object], message: str
    ) -> None:
        """Partial matches, bad regexes and long values fail."""
        with pytest.raises(ValidationError, match=message):
            StringArtifact.model_validate({"id": "s", "path": "/s", **fields})


@pytest.mark.unit
class TestValueLimits:
    """The limits added on top of ranges: lengths, exclusions, schema docs."""

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"min_length": 3, "value": "ab"}, "at least 3 characters"),
            ({"min_length": 3, "max_length": 2}, "more than the at most"),
            ({"exclude": ["changeme"], "value": "changeme"}, "not allowed"),
        ],
    )
    def test_string_limits_reject(
        self, fields: dict[str, object], message: str
    ) -> None:
        """Short, inverted and excluded strings fail."""
        with pytest.raises(ValidationError, match=message):
            StringArtifact.model_validate({"id": "s", "path": "/s", **fields})

    def test_string_limits_accept(self) -> None:
        """A value inside every limit is valid."""
        StringArtifact.model_validate(
            {
                "id": "s",
                "path": "/s",
                "min_length": 2,
                "max_length": 4,
                "exclude": ["x"],
                "value": "abc",
            }
        )

    def test_number_exclude(self) -> None:
        """An excluded number fails even inside the range."""
        NumberArtifact.model_validate(
            {"id": "n", "path": "/n", "exclude": [0], "value": 1}
        )
        with pytest.raises(ValidationError, match="not allowed"):
            NumberArtifact.model_validate(
                {"id": "n", "path": "/n", "min": -1, "exclude": [0]}
                | {"value": 0}
            )

    def test_schema_carries_field_docs(self) -> None:
        """Attribute docstrings reach the JSON schema the GUI renders."""
        props = NumberArtifact.model_json_schema()["properties"]
        assert "smallest allowed value" in props["min"]["description"]
