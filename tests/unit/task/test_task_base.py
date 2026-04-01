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
Unit tests for BaseTask abstract base class.
"""

import inspect
from abc import ABC
from typing import ClassVar

import pytest
from pydantic import BaseModel, ValidationError

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_runtime.core.task.base import BaseTask
from horus_runtime.registry.auto_registry import (
    AutoRegistry,
)
from horus_runtime.registry.exceptions import RegistryKeyIsNoneError


class ConcreteTestTask(BaseTask):
    """
    A concrete implementation of BaseTask for testing purposes.
    """

    kind: str = "test_task"

    async def run(self) -> None:
        """
        Test implementation of run method.
        """

    def is_complete(self) -> bool:
        """
        Always return True for testing purposes.
        """
        return True

    def reset(self) -> None:
        """
        Do nothing for testing purposes.
        """
        pass


@pytest.mark.unit
class TestBaseTask:
    """
    Test cases for BaseTask abstract base class.
    """

    def test_base_task_is_abstract(self) -> None:
        """
        Test that BaseTask cannot be instantiated directly.
        """
        with pytest.raises(TypeError):
            BaseTask()  # type: ignore

    def test_base_task_inherits_correctly(self) -> None:
        """
        Test that BaseTask inherits from expected classes.
        """
        # This is a little bit redundant since we know BaseTask is defined
        # as inheriting from these, but it serves as a sanity check that the
        # class hierarchy is correct. Also can signal for future refactors if
        # the inheritance changes.
        assert issubclass(BaseTask, BaseModel)
        assert issubclass(BaseTask, ABC)
        assert issubclass(BaseTask, AutoRegistry)

    def test_registry_key_is_kind(self) -> None:
        """
        Test that BaseTask uses 'kind' as registry key.
        """
        # This check will be done for other classes that inherit from
        # autoregistry. For task, the registry key is 'kind',
        # so we want to make sure that this is set correctly in the base class.
        assert BaseTask.registry_key == "kind"

    def test_run_method_is_abstract(self) -> None:
        """
        Test that run method is marked as abstract.
        """
        # Check that the run method is in the abstract methods
        abstract_methods = BaseTask.__abstractmethods__
        assert "run" in abstract_methods

    def test_run_method_signature(self) -> None:
        """
        Test that run method has correct signature.
        """
        sig = inspect.signature(BaseTask.run)

        params = list(sig.parameters.keys())
        assert params == ["self"]

    def test_base_task_has_required_fields(self) -> None:
        """
        Test that BaseTask has all required fields defined.
        """
        fields = BaseTask.model_fields

        # Check that all expected fields are present
        expected_fields = {
            "kind",
            "inputs",
            "outputs",
            "variables",
            "executor",
            "runtime",
        }
        assert expected_fields.issubset(fields.keys())

    def test_default_field_values(self) -> None:
        """
        Test that BaseTask fields have correct default values.
        """
        # Create a concrete task to test defaults
        task = ConcreteTestTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo test"),
        )

        # Test default values
        assert not task.inputs
        assert not task.outputs
        assert not task.variables
        assert task.kind == "test_task"


@pytest.mark.unit
class TestBaseTaskValidation:
    """
    Test cases for validating BaseTask behavior with a concrete
    implementation.
    """

    def test_kind_field_must_be_set_in_subclass(self) -> None:
        """
        Test that a subclass of BaseTask must set the 'kind' field.
        """
        with pytest.raises(RegistryKeyIsNoneError):

            class InvalidTask(BaseTask):
                """
                Invalid task that does not set 'kind' field.
                """

                def is_complete(self) -> bool:
                    return True

                def reset(self) -> None:
                    pass

                async def run(self) -> None:
                    pass

    def test_model_validation_preserves_type_safety(self) -> None:
        """
        Test that model validation on a BaseTask preserves type safety.
        """
        with pytest.raises(ValidationError):
            ConcreteTestTask(
                kind=123,  # type: ignore
                executor=ShellExecutor(),
                runtime=CommandRuntime(command="echo test"),
            )

    def test_abstract_method_enforcement(self) -> None:
        """
        Test that subclasses must implement abstract methods.
        """
        with pytest.raises(
            TypeError, match="Can't instantiate abstract class"
        ):

            class IncompleteTask(BaseTask):
                """
                Incomplete task that doesn't implement run method.
                """

                add_to_registry: ClassVar[bool] = False
                kind: str = "incomplete"
                # Missing run method implementation

            # This should fail because run method is not implemented
            IncompleteTask(  # type: ignore
                executor=ShellExecutor(),
                runtime=CommandRuntime(command="echo test"),
            )

    def test_required_fields_validation(self) -> None:
        """
        Test that required fields are properly validated.
        """
        # Test missing executor
        with pytest.raises(ValidationError):
            ConcreteTestTask(  # type: ignore[call-arg]
                name="test_task",
                runtime=CommandRuntime(command="echo test"),
            )

        # Test missing runtime
        with pytest.raises(ValidationError):
            ConcreteTestTask(  # type: ignore[call-arg]
                name="test_task",
                executor=ShellExecutor(),
            )

    def test_valid_task_creation(self) -> None:
        """
        Test that a valid task can be created successfully.
        """
        task = ConcreteTestTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo test"),
            inputs={},
            outputs={},
            variables={"test_var": "test_value"},
        )

        assert task.kind == "test_task"
        assert isinstance(task.executor, ShellExecutor)
        assert isinstance(task.runtime, CommandRuntime)
        assert task.variables["test_var"] == "test_value"

    def test_task_with_artifacts(self) -> None:
        """
        Test that a task can be created with input and output artifacts.
        """
        input_artifact = FileArtifact(uri="test_input.txt")
        output_artifact = FileArtifact(uri="test_output.txt")

        task = ConcreteTestTask(
            name="test_task",
            executor=ShellExecutor(),
            runtime=CommandRuntime(command="echo test"),
            inputs={"input1": input_artifact},
            outputs={"output1": output_artifact},
        )

        assert "input1" in task.inputs
        assert "output1" in task.outputs
        assert isinstance(task.inputs["input1"], FileArtifact)
        assert isinstance(task.outputs["output1"], FileArtifact)
