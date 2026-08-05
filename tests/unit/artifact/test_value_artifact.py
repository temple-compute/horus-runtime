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

import pytest
from pydantic import BaseModel

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

    def test_materialize_is_idempotent_and_does_not_clobber(self) -> None:
        """An existing backing file always wins over the authored value."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "s.txt"
            target.write_text("uploaded bytes")
            artifact = StringArtifact(id="s", path=target, value="edited")
            artifact.materialize()
            assert target.read_text() == "uploaded bytes"

    def test_base_artifact_materialize_is_noop(self) -> None:
        """BaseArtifact.materialize() is a no-op for non-value artifacts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "f.txt"
            artifact = BaseArtifact.model_validate(
                {"id": "f", "kind": "file", "path": str(target)}
            )
            artifact.materialize()
            assert not target.exists()
