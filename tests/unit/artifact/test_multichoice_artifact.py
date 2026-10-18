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
Tests for MultiChoiceArtifact (any subset of authored options) and
ListArtifact (a free list of scalars): both render into the task as
space-separated, shell-quoted words.
"""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.list import ListArtifact
from horus_builtin.artifact.multichoice import MultiChoiceArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.substitution import substitute
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.workflow.edge import WorkflowEdge

ELEMENTS = [{"value": "H"}, {"value": "C"}, {"value": "O", "label": "Oxygen"}]


def _elements(path: Path, **kwargs: object) -> MultiChoiceArtifact:
    return MultiChoiceArtifact.model_validate(
        {"id": "elements", "path": path, "options": ELEMENTS, **kwargs}
    )


def _task(*inputs: BaseArtifact) -> HorusTask:
    return HorusTask(
        id="t",
        name="t",
        runtime=CommandRuntime(command="true"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=list(inputs),
    )


@pytest.mark.unit
class TestMultiChoice:
    """The options are a valid domain and the value a subset of them."""

    def test_registered_and_round_trips_by_kind(self) -> None:
        """Dumps and reloads through the discriminated artifact union."""

        class Doc(BaseModel):
            artifact: BaseArtifact

        doc = Doc.model_validate(
            {
                "artifact": {
                    "id": "elements",
                    "kind": "multichoice",
                    "path": "/e",
                    "options": ELEMENTS,
                    "value": ["O", "H"],
                }
            }
        )
        assert BaseArtifact.registry["multichoice"] is MultiChoiceArtifact
        reloaded = Doc.model_validate(doc.model_dump(mode="json"))
        assert cast(MultiChoiceArtifact, reloaded.artifact).value == ["H", "O"]

    def test_defaults_to_no_pick(self, tmp_path: Path) -> None:
        """An empty selection is valid."""
        assert _elements(tmp_path / "e").value == []

    def test_value_is_normalized(self, tmp_path: Path) -> None:
        """Option order, no duplicates, the option's own type."""
        artifact = MultiChoiceArtifact.model_validate(
            {
                "id": "n",
                "path": tmp_path / "n",
                "options": [{"value": 1}, {"value": 2}, {"value": 3}],
                "value": ["3", 1, 3],
            }
        )
        assert artifact.value == [1, 3]

    @pytest.mark.parametrize(
        ("options", "value", "message"),
        [
            ([], [], "needs at least one option"),
            ([{"value": "a"}, {"value": "a"}], [], "duplicate option 'a'"),
            (ELEMENTS, ["N"], "'N' is not one of its options"),
        ],
    )
    def test_invalid_domains_are_rejected(
        self,
        tmp_path: Path,
        options: list[dict[str, object]],
        value: list[object],
        message: str,
    ) -> None:
        """Empty, duplicate and out-of-domain cases fail with a clear error."""
        with pytest.raises(ValidationError, match=message):
            MultiChoiceArtifact.model_validate(
                {
                    "id": "e",
                    "path": tmp_path / "e",
                    "options": options,
                    "value": value,
                }
            )

    def test_disk_is_a_json_array_checked_both_ways(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Written and read as JSON, every item one of the options."""
        del horus_context
        artifact = _elements(tmp_path / "e", value=["C", "H"])
        artifact.materialize()
        assert json.loads(artifact.path.read_text()) == ["H", "C"]
        assert artifact.read() == ["H", "C"]
        with pytest.raises(ValueError, match="not one of its options"):
            artifact.write(["N"])
        artifact.path.write_text('["N"]')
        with pytest.raises(ValueError, match="not one of its options"):
            artifact.read()


@pytest.mark.unit
class TestListArtifact:
    """A free list of scalars, JSON on disk."""

    def test_registered_and_defaults_empty(self, tmp_path: Path) -> None:
        """Registers as ``list`` and starts empty."""
        assert BaseArtifact.registry["list"] is ListArtifact
        assert ListArtifact(id="l", path=tmp_path / "l").value == []

    def test_write_then_read_round_trips(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Items keep their JSON types on disk."""
        del horus_context
        artifact = ListArtifact(id="l", path=tmp_path / "l", value=[1, "a b"])
        artifact.materialize()
        assert artifact.path.read_text() == '[1, "a b"]'
        assert artifact.read() == [1, "a b"]


@pytest.mark.unit
class TestListRendering:
    """``$id`` gives one shell word per item, whether or not it is quoted."""

    @pytest.mark.parametrize("quote", [False, True])
    def test_items_render_as_separate_words(
        self, tmp_path: Path, quote: bool
    ) -> None:
        """Quoting the whole value would fuse the words into one."""
        tags = ListArtifact(id="tags", path=tmp_path / "t", value=["a", "b c"])
        assert substitute("x $tags", _task(tags), quote=quote) == "x a 'b c'"

    def test_multichoice_renders_values_not_labels(
        self, tmp_path: Path
    ) -> None:
        """Picked values render in option order, never labels."""
        artifact = _elements(tmp_path / "e", value=["O", "H"])
        assert substitute("$elements", _task(artifact)) == "H O"

    def test_empty_list_renders_nothing(self, tmp_path: Path) -> None:
        """No items, no argument."""
        tags = ListArtifact(id="tags", path=tmp_path / "t")
        assert substitute("x $tags", _task(tags), quote=True) == "x "


@pytest.mark.unit
async def test_root_list_reaches_the_task(
    tmp_path: Path, horus_context: HorusContext
) -> None:
    """A root list materializes and its items reach the shell as words."""
    del horus_context
    root = ListArtifact(id="tags", path=Path("roots/tags"), value=["a", "b c"])
    task = HorusTask(
        id="t",
        name="t",
        runtime=CommandRuntime(command="printf '%s|' $tags > $report"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        inputs=[ListArtifact(id="tags", path=Path("tags"))],
        outputs=[FileArtifact(id="report", path=Path("report.txt"))],
    )
    wf = HorusWorkflow(
        name="list",
        tasks=[task],
        artifacts=[root],
        edges=[
            WorkflowEdge(
                source="artifact-tags",
                source_output="tags",
                target="t",
                target_input="tags",
            )
        ],
    )
    wf._base_directory = tmp_path

    await wf.run(trigger_id="t")

    assert wf.status.value == "completed"
    assert json.loads((tmp_path / "roots/tags").read_text()) == ["a", "b c"]
    assert wf.tasks[0].outputs[0].path.read_text() == "a|b c|"
