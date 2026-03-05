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
Unit tests for the Workflow class
"""

import textwrap
from pathlib import Path
from typing import ClassVar

import pytest
import yaml
from pydantic import ValidationError

from horus_runtime.core.workflow.base import BaseWorkflow
from tests.conftest import MakeWorkflowFileType


class ConcreteWorkflow(BaseWorkflow):
    kind: ClassVar[str] = "concrete_workflow"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ConcreteWorkflow":
        with Path(path).open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
            return cls.model_validate(data)

    def run(self) -> None:
        print(
            f"Running workflow '{self.name}' with {len(self.tasks)} tasks..."
        )

    def reset(self) -> None:
        return None


@pytest.mark.unit
class TestWorkflowFromYaml:
    def test_from_yaml_loads_valid_file(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Test that a valid workflow YAML file is correctly loaded into a
        Workflow object.
        """

        wf_content = textwrap.dedent("""\
        name: yaml_workflow
        kind: concrete_workflow
        tasks:
            step1:
                name: Step 1
                kind: horus_task
                runtime:
                    kind: command
                    command: "echo hello"
                executor:
                    kind: shell
        """)

        workflow_file = make_workflow_file(tmp_path, wf_content)
        wf = ConcreteWorkflow.from_yaml(workflow_file)

        assert wf.name == "yaml_workflow"
        assert "step1" in wf.tasks

    def test_from_yaml_accepts_string_path(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Test that from_yaml can accept a string path as well as a Path object.
        """
        wf_contents = textwrap.dedent("""\
        name: str_path
        kind: concrete_workflow
        tasks:
            t1:
                name: Task 1
                kind: horus_task
                runtime:
                    kind: command
                    command: "echo hi"
                executor:
                    kind: shell
        """)

        workflow_file = make_workflow_file(tmp_path, wf_contents)
        wf = ConcreteWorkflow.from_yaml(str(workflow_file))
        assert wf.name == "str_path"
        assert "t1" in wf.tasks

    def test_from_yaml_invalid_schema_raises(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Unknown task kind should trigger a ValidationError.
        """
        bad_schema = textwrap.dedent("""\
        name: bad
        tasks:
            t1:
                kind: definitely_not_a_registered_kind
                runtime:
                    kind: command
                    command: "echo"
                executor:
                    kind: shell
        """)

        wf_file = make_workflow_file(tmp_path, bad_schema)

        with pytest.raises(ValidationError):
            ConcreteWorkflow.from_yaml(wf_file)
