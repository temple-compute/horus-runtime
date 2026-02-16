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
Unit tests for BaseExecutor class
"""

import inspect
from abc import ABC
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.registry.auto_registry import (
    AutoRegistry,
    RegistryKeyIsNoneError,
)


class ConcreteTestExecutor(BaseExecutor):
    """
    Concrete implementation of BaseExecutor for testing purposes.
    """

    add_to_registry = False  # Prevent registry pollution in tests
    kind: Literal["test"] = "test"

    def execute(self, cmd: str) -> int:
        """
        Simple test implementation that returns success for non-empty commands.
        """
        return 0 if cmd.strip() else 1


@pytest.mark.unit
class TestBaseExecutor:
    """
    Test cases for BaseExecutor abstract base class
    """

    def test_base_executor_is_abstract(self) -> None:
        """
        Test that BaseExecutor cannot be instantiated directly
        """
        with pytest.raises(
            TypeError, match="Can't instantiate abstract class"
        ):
            # We use type:ignore because the linter correctly identifies that
            # BaseExecutor is abstract and cannot be instantiated, but we want
            # to test this behavior explicitly at runtime.
            BaseExecutor()  # type: ignore

    def test_base_executor_inherits_correctly(self) -> None:
        """
        Test that BaseExecutor inherits from expected classes
        """
        # This is a little bit redundant since we know BaseExecutor is defined
        # as inheriting from these, but it serves as a sanity check that the
        # class hierarchy is correct. Also can signal for future refactors if
        # the inheritance changes.
        assert issubclass(BaseExecutor, BaseModel)
        assert issubclass(BaseExecutor, ABC)
        assert issubclass(BaseExecutor, AutoRegistry)

    def test_registry_key_is_kind(self) -> None:
        """
        Test that BaseExecutor uses 'kind' as registry key
        """
        # This check will be done for other classes that inherit from
        # autoregistry. For executor, the registry key is 'kind',
        # so we want to make sure that this is set correctly in the base class.
        assert BaseExecutor.registry_key == "kind"

    def test_execute_method_is_abstract(self) -> None:
        """
        Test that execute method is marked as abstract
        """
        # Check that the execute method is in the abstract methods
        abstract_methods = BaseExecutor.__abstractmethods__
        assert "execute" in abstract_methods

    def test_concrete_executor_implementation(self) -> None:
        """
        Test that concrete implementation works correctly
        """
        executor = ConcreteTestExecutor()

        # Test successful execution
        result = executor.execute("echo test")
        assert result == 0

        # Test failure case
        result = executor.execute("")
        assert result == 1

    def test_kind_field_validation(self) -> None:
        """
        Test that kind field validation works in subclasses
        """
        # This should work since ConcreteTestExecutor sets kind = "test"
        executor = ConcreteTestExecutor()
        assert executor.kind == "test"

    def test_executor_serialization(self) -> None:
        """
        Test that executors can be serialized to dict
        """
        executor = ConcreteTestExecutor()
        executor_dict = executor.model_dump()

        assert "kind" in executor_dict
        assert executor_dict["kind"] == "test"

    def test_executor_deserialization(self) -> None:
        """
        Test that executors can be deserialized from dict
        """
        data = {"kind": "test"}

        executor = ConcreteTestExecutor.model_validate(data)

        assert executor.kind == "test"

    def test_execute_method_signature(self) -> None:
        """
        Test that execute method has correct signature
        """

        sig = inspect.signature(BaseExecutor.execute)

        # Should have 'self' and 'cmd' parameters
        params = list(sig.parameters.keys())
        assert params == ["self", "cmd"]

        # Check parameter types
        cmd_param = sig.parameters["cmd"]
        assert cmd_param.annotation == str

        # Check return type
        assert sig.return_annotation == int


@pytest.mark.unit
class TestBaseExecutorValidation:
    """
    Test validation behavior of BaseExecutor
    """

    def test_kind_field_must_be_set_in_subclass(self) -> None:
        """
        Test that subclasses must set the kind field
        """
        # This executor doesn't set kind, so validation should fail
        with pytest.raises(
            RegistryKeyIsNoneError,
            match="must define a class property named 'kind'",
        ):

            class InvalidExecutorNoKind(  # pyright: ignore[reportUnusedClass]
                BaseExecutor
            ):
                """
                Invalid executor implementation without kind field for testing
                """

                add_to_registry = True

                def execute(self, cmd: str) -> int:
                    return 0

    def test_model_validation_preserves_type_safety(self) -> None:
        """
        Test that Pydantic validation maintains type safety
        """
        with pytest.raises(ValidationError):
            # We use type:ignore here because we're intentionally passing
            # the wrong type for the 'kind' field. The linter will complain
            # about this, but we want to ensure that the validation error
            # is raised at runtime.
            ConcreteTestExecutor(kind=123)  # type: ignore

    def test_extra_fields_handling(self) -> None:
        """
        Test behavior with extra fields in model validation
        """
        data = {
            "kind": "test",
            "extra_field": "should_be_ignored",
        }

        # Should work fine - extra fields are ignored by default
        executor = ConcreteTestExecutor.model_validate(data)
        assert executor.kind == "test"

    def test_abstract_method_enforcement(self) -> None:
        """
        Test that subclasses must implement abstract methods
        """
        with pytest.raises(
            TypeError, match="Can't instantiate abstract class"
        ):

            class IncompleteExecutor(  # pyright: ignore[reportUnusedClass]
                BaseExecutor
            ):
                """
                Incomplete executor that doesn't implement execute method
                """

                add_to_registry = False
                kind: Literal["incomplete"] = "incomplete"
                # Missing execute method implementation

            # This should fail because execute method is not implemented
            IncompleteExecutor()  # type: ignore
