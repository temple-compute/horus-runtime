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
Unit tests for BaseRuntime abstract base class
"""

import inspect
from abc import ABC
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

from horus_runtime.core.registry.auto_registry import (
    AutoRegistry,
    RegistryKeyIsNoneError,
)
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.task.base import BaseTask


class ConcreteTestRuntime(BaseRuntime):
    """
    A concrete implementation of BaseRuntime for testing purposes.
    """

    kind: Literal["test_runtime"] = "test_runtime"

    def _setup_runtime(self, task: "BaseTask") -> str:
        return "Runtime setup complete"


@pytest.mark.unit
class TestBaseRuntime:
    """
    Test cases for BaseRuntime abstract base class
    """

    def test_base_runtime_is_abstract(self) -> None:
        """
        Test that BaseRuntime cannot be instantiated directly
        """
        with pytest.raises(TypeError):
            BaseRuntime()  # type: ignore

    def test_base_runtime_inherits_correctly(self) -> None:
        """
        Test that BaseRuntime inherits from expected classes
        """
        # This is a little bit redundant since we know BaseRuntime is defined
        # as inheriting from these, but it serves as a sanity check that the
        # class hierarchy is correct. Also can signal for future refactors if
        # the inheritance changes.
        assert issubclass(BaseRuntime, BaseModel)
        assert issubclass(BaseRuntime, ABC)
        assert issubclass(BaseRuntime, AutoRegistry)

    def test_registry_key_is_kind(self) -> None:
        """
        Test that BaseRuntime uses 'kind' as registry key
        """
        # This check will be done for other classes that inherit from
        # autoregistry. For runtime, the registry key is 'kind',
        # so we want to make sure that this is set correctly in the base class.
        assert BaseRuntime.registry_key == "kind"

    def test_setup_runtime_method_is_abstract(self) -> None:
        """
        Test that _setup_runtime method is marked as abstract
        """
        # Check that the _setup_runtime method is in the abstract methods
        abstract_methods = BaseRuntime.__abstractmethods__
        assert "_setup_runtime" in abstract_methods

    def test_setup_runtime_signature(self) -> None:
        """
        Test that _setup_runtime method has correct signature
        """

        sig = inspect.signature(
            BaseRuntime._setup_runtime  # pylint: disable=protected-access
        )

        params = list(sig.parameters.keys())
        assert params == ["self", "task"]

        # Check parameter types
        task_param = sig.parameters["task"]
        assert task_param.annotation == "BaseTask"

        # Check return type
        assert sig.return_annotation is str


@pytest.mark.unit
class TestBaseRuntimeValidation:
    """
    Test cases for validating BaseRuntime behavior with a concrete
    implementation
    """

    def test_kind_field_must_be_set_in_subclass(self) -> None:
        """
        Test that a subclass of BaseRuntime must set the 'kind' field
        """
        with pytest.raises(RegistryKeyIsNoneError):

            class InvalidRuntime(  # pyright: ignore[reportUnusedClass]
                BaseRuntime
            ):
                """
                Invalid runtime that does not set 'kind' field
                """

                def _setup_runtime(self, task: "BaseTask") -> str:
                    return ""

    def test_model_validation_preserves_type_safety(self) -> None:
        """
        Test that model validation on a BaseRuntime preserves type safety
        """

        with pytest.raises(ValidationError):
            ConcreteTestRuntime(kind=123)  # type: ignore

    def test_abstract_method_enforcement(self) -> None:
        """
        Test that subclasses must implement abstract methods
        """
        with pytest.raises(
            TypeError, match="Can't instantiate abstract class"
        ):

            class IncompleteRuntime(  # pyright: ignore[reportUnusedClass]
                BaseRuntime
            ):
                """
                Incomplete runtime that doesn't implement _setup_runtime method
                """

                add_to_registry = False
                kind: Literal["incomplete"] = "incomplete"
                # Missing _setup_runtime method implementation

            # This should fail because _setup_runtime method is not implemented
            IncompleteRuntime()  # type: ignore
