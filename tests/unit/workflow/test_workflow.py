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
Unit tests for the Workflow class.
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
    """
    A concrete implementation of BaseWorkflow for testing purposes.
    """

    kind: str = "concrete_workflow"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ConcreteWorkflow":
        """
        Load a workflow from a YAML file and return an instance of
        ConcreteWorkflow.
        """
        with Path(path).open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
            return cls.model_validate(data)

    async def _run(self, trigger_id: str) -> None:
        """
        Run the workflow by executing all its tasks.
        """
        del trigger_id

    async def _reset(self) -> None:
        """
        Reset the workflow to its initial state.
        """
        return None


@pytest.mark.unit
class TestWorkflowFromYaml:
    """
    Tests for the from_yaml class method of the Workflow class.
    """

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
            - id: step1_id
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
        assert "step1_id" in [t.id for t in wf.tasks]

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
            - id: t1_id
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
        assert "t1_id" in [t.id for t in wf.tasks]

    def test_from_yaml_invalid_schema_raises(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Unknown task kind should trigger a ValidationError.
        """
        bad_schema = textwrap.dedent("""\
        name: bad
        tasks:
            - id: t1
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


class SourcePathWorkflow(BaseWorkflow):
    """
    A concrete workflow that inherits ``from_yaml`` rather than
    overriding it, so the loader under test actually runs.
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "source_path_workflow"

    async def _run(self, trigger_id: str) -> None:
        """
        No-op body.
        """
        del trigger_id

    async def _reset(self) -> None:
        """
        No-op reset.
        """
        return None


class TestSourcePath:
    """
    The file a workflow was loaded from.
    """

    WORKFLOW = textwrap.dedent("""\
    name: yaml_workflow
    kind: source_path_workflow
    tasks:
        - id: step1_id
          name: Step 1
          kind: horus_task
          runtime:
              kind: command
              command: "echo hello"
          executor:
              kind: shell
    """)

    def test_from_yaml_keeps_the_file_path(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Deriving it from the base directory is only correct when the file
        happens to be named workflow.yaml.
        """
        workflow_file = make_workflow_file(tmp_path, self.WORKFLOW)
        wf = SourcePathWorkflow.from_yaml(workflow_file)
        assert wf.source_path == workflow_file.resolve()

    def test_the_base_directory_still_points_at_the_folder(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        The new field sits beside the existing one, it does not replace
        it.
        """
        workflow_file = make_workflow_file(tmp_path, self.WORKFLOW)
        wf = SourcePathWorkflow.from_yaml(workflow_file)
        assert wf.source_path is not None
        assert wf.source_path.parent == wf._effective_base

    def test_it_is_none_without_a_file(self) -> None:
        """
        A workflow built in Python has no source to point at.
        """
        assert SourcePathWorkflow(name="built_in_python").source_path is None

    def test_it_is_not_serialized(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Runtime-only state, like the base directory beside it, so a
        snapshot's meaning does not change.
        """
        workflow_file = make_workflow_file(tmp_path, self.WORKFLOW)
        wf = SourcePathWorkflow.from_yaml(workflow_file)
        assert "source_path" not in wf.model_dump(mode="json")
