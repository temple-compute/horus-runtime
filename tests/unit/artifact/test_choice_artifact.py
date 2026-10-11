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
Tests for ChoiceArtifact: a value picked from authored ``{value, label}``
options, rendered into the task as the picked *value*.
"""

import re
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.artifact.choice import ChoiceArtifact
from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.substitution import substitute
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.workflow.edge import WorkflowEdge

DEVICE = [
    {"value": "cpu", "label": "CPU"},
    {"value": "gpu", "label": "GPU"},
]


def _device(path: Path, **kwargs: object) -> ChoiceArtifact:
    return ChoiceArtifact.model_validate(
        {"id": "device", "path": path, "options": DEVICE, **kwargs}
    )


@pytest.mark.unit
class TestChoiceValidation:
    """The options are a valid domain and the value is one of them."""

    def test_registered_and_round_trips_by_kind(self) -> None:
        """Dumps and reloads through the discriminated artifact union."""

        class Doc(BaseModel):
            artifact: BaseArtifact

        doc = Doc.model_validate(
            {
                "artifact": {
                    "id": "device",
                    "kind": "choice",
                    "path": "/d",
                    "options": DEVICE,
                    "value": "gpu",
                }
            }
        )
        artifact = cast(ChoiceArtifact, doc.artifact)
        assert BaseArtifact.registry["choice"] is ChoiceArtifact
        assert artifact.value == "gpu"
        assert artifact.options[1].label == "GPU"
        reloaded = Doc.model_validate(doc.model_dump(mode="json"))
        assert cast(ChoiceArtifact, reloaded.artifact).value == "gpu"

    def test_value_defaults_to_first_option(self, tmp_path: Path) -> None:
        """No value selects the first option."""
        assert _device(tmp_path / "d").value == "cpu"

    def test_label_is_optional(self, tmp_path: Path) -> None:
        """An option without a label is just its value."""
        artifact = ChoiceArtifact(
            id="n",
            path=tmp_path / "n",
            options=[{"value": 4}],  # type: ignore[list-item]
        )
        assert artifact.options[0].label is None
        assert artifact.value == 4

    @pytest.mark.parametrize(
        ("options", "value", "message"),
        [
            ([], None, "needs at least one option"),
            (
                [{"value": "a"}, {"value": "a", "label": "again"}],
                None,
                "duplicate option 'a'",
            ),
            # Values are distinct by their rendered text: a file holding
            # ``true`` must read back to exactly one option.
            ([{"value": True}, {"value": "true"}], None, "duplicate"),
            (
                DEVICE,
                "tpu",
                re.escape("'tpu' is not one of its options (cpu, gpu)"),
            ),
        ],
    )
    def test_invalid_domains_are_rejected(
        self,
        tmp_path: Path,
        options: list[dict[str, object]],
        value: object,
        message: str,
    ) -> None:
        """Empty, duplicate and out-of-domain cases fail with a clear error."""
        with pytest.raises(ValidationError, match=message):
            ChoiceArtifact.model_validate(
                {
                    "id": "d",
                    "path": tmp_path / "d",
                    "options": options,
                    "value": value,
                }
            )

    def test_value_is_normalized_to_the_option(self, tmp_path: Path) -> None:
        """A value matching an option's text takes the option's own type."""
        artifact = ChoiceArtifact.model_validate(
            {
                "id": "t",
                "path": tmp_path / "t",
                "options": [{"value": 1}, {"value": 2}],
                "value": "2",
            }
        )
        assert artifact.value == 2


@pytest.mark.unit
class TestChoiceSerialization:
    """On disk and in ``$id`` the choice is its value's text."""

    def test_string_written_bare_and_read_back(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Strings are plain text, so ``cat $device`` matches ``$device``."""
        del horus_context
        artifact = _device(tmp_path / "d", value="gpu")
        artifact.materialize()
        assert artifact.path.read_text() == "gpu"
        assert artifact.read() == "gpu"

    def test_non_string_values_round_trip_typed(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """Numbers and booleans are JSON on disk and read back typed."""
        del horus_context
        artifact = ChoiceArtifact.model_validate(
            {
                "id": "x",
                "path": tmp_path / "x",
                "options": [{"value": 0.5}, {"value": True}],
            }
        )
        artifact.write(True)
        assert artifact.path.read_text() == "true"
        assert artifact.read() is True

    def test_write_and_read_reject_values_outside_the_options(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """A producer cannot write, nor a file hold, an undeclared value."""
        del horus_context
        artifact = _device(tmp_path / "d")
        with pytest.raises(ValueError, match="not one of its options"):
            artifact.write("tpu")
        artifact.path.write_text("tpu")
        with pytest.raises(ValueError, match="not one of its options"):
            artifact.read()

    def test_renders_value_not_label_in_command(self, tmp_path: Path) -> None:
        """``--device $device`` receives the value, never the label."""
        artifact = _device(tmp_path / "d", value="gpu")
        task = HorusTask(
            id="t",
            name="t",
            runtime=CommandRuntime(command="run --device $device"),
            executor=ShellExecutor(),
            target=LocalTarget(),
            inputs=[artifact],
        )
        assert substitute("run --device $device", task) == "run --device gpu"


@pytest.mark.unit
async def test_root_choice_reaches_the_task(
    tmp_path: Path, horus_context: HorusContext
) -> None:
    """A root choice materializes and its picked value reaches the command."""
    del horus_context
    root = _device(Path("roots/device"), value="gpu")
    task = HorusTask(
        id="t",
        name="t",
        runtime=CommandRuntime(command="echo --device $device > $report"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        # The consumer's own declaration starts at the default (cpu); the
        # root's picked value must replace it.
        inputs=[_device(Path("device"))],
        outputs=[FileArtifact(id="report", path=Path("report.txt"))],
    )
    wf = HorusWorkflow(
        name="choice",
        tasks=[task],
        artifacts=[root],
        edges=[
            WorkflowEdge(
                source="artifact-device",
                source_output="device",
                target="t",
                target_input="device",
            )
        ],
    )
    wf._base_directory = tmp_path

    await wf.run(trigger_id="t")

    assert wf.status.value == "completed"
    assert (tmp_path / "roots/device").read_text() == "gpu"
    report = wf.tasks[0].outputs[0]
    assert report.path.read_text().strip() == "--device gpu"
