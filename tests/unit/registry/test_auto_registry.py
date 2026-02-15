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
Unit tests for AutoRegistry class
"""

from typing import ClassVar, Literal

import pytest

from horus_runtime.core.registry.auto_registry import (
    AutoRegistry,
    NoSubclassesRegisteredError,
    RegistryKeyAttributeNotDefined,
    RegistryKeyIsNoneError,
    init_registry,
)


class TestRegistryBase(AutoRegistry):
    """
    Test base class for AutoRegistry testing
    """

    registry_key: ClassVar[str] = "test_type"
    add_to_registry: ClassVar[bool] = False  # Prevent pollution


class ConcreteRegistryItem(TestRegistryBase):
    """
    Concrete implementation for testing
    """

    test_type: Literal["concrete"] = "concrete"
    add_to_registry: ClassVar[bool] = True


class AnotherConcreteItem(TestRegistryBase):
    """
    Another concrete implementation for testing
    """

    test_type: Literal["another"] = "another"
    add_to_registry: ClassVar[bool] = True


class AbstractTestItem(TestRegistryBase):
    """
    Abstract class that should not be registered
    """

    test_type: Literal["abstract"] = "abstract"
    add_to_registry: ClassVar[bool] = False


@pytest.mark.unit
class TestAutoRegistry:
    """
    Test cases for AutoRegistry class
    """

    def test_registry_initialization(self) -> None:
        """
        Test that registry is properly initialized for subclasses
        """
        assert hasattr(TestRegistryBase, "registry")
        assert isinstance(TestRegistryBase.registry, dict)

    def test_concrete_items_registered(self) -> None:
        """
        Test that concrete items are registered when add_to_registry is True
        """

        assert "concrete" in ConcreteRegistryItem.registry
        assert "another" in ConcreteRegistryItem.registry
        assert (
            ConcreteRegistryItem.registry["concrete"] == ConcreteRegistryItem
        )
        assert ConcreteRegistryItem.registry["another"] == AnotherConcreteItem

    def test_abstract_items_not_registered(self) -> None:
        """
        Test that abstract items are not registered when add_to_registry is
        False
        """

        # Should not be in registry
        assert "abstract" not in AbstractTestItem.registry

    def test_registry_separation_between_hierarchies(self) -> None:
        """
        Test that different inheritance hierarchies have separate registries
        """

        class DifferentBase(AutoRegistry):
            """
            Different base class for testing separate registries
            """

            registry_key: ClassVar[str] = "different_key"
            add_to_registry: ClassVar[bool] = False

        class DifferentConcrete(  # pyright: ignore[reportUnusedClass]
            DifferentBase
        ):
            """
            Test class
            """

            different_key: Literal["diff"] = "diff"
            add_to_registry: ClassVar[bool] = True

        # Should have separate registries
        assert hasattr(TestRegistryBase, "registry")
        assert hasattr(DifferentBase, "registry")
        assert TestRegistryBase.registry is not DifferentBase.registry

    def test_registry_key_from_attribute(self) -> None:
        """
        Test that registry_key value is taken from the class attribute
        """

        class TestKeyItem(AutoRegistry):
            """
            Test class for registry key from attribute
            """

            registry_key: ClassVar[str] = "my_key"
            my_key: Literal["test_value"] = "test_value"
            add_to_registry: ClassVar[bool] = True

        assert "test_value" in TestKeyItem.registry
        assert TestKeyItem.registry["test_value"] == TestKeyItem

    def test_multiple_inheritance_registry(self) -> None:
        """
        Test registry behavior with multiple inheritance scenarios
        """

        class MixinClass:
            """
            Mixin class for testing
            """

            mixin_attr: str = "mixin"

        class MultiInheritItem(TestRegistryBase, MixinClass):
            """
            Test class for multiple inheritance scenarios
            """

            test_type: Literal["multi"] = "multi"
            add_to_registry: ClassVar[bool] = True

        assert "multi" in MultiInheritItem.registry
        assert MultiInheritItem.registry["multi"] == MultiInheritItem

    def test_registry_overwrite_protection(self) -> None:
        """
        Test behavior when multiple classes try to register with same key
        """

        class FirstItem(TestRegistryBase):
            """
            First class to register
            """

            test_type: Literal["duplicate"] = "duplicate"
            add_to_registry: ClassVar[bool] = True

        first_class = FirstItem.registry["duplicate"]

        class SecondItem(TestRegistryBase):
            """
            Second class to register
            """

            test_type: Literal["duplicate"] = "duplicate"
            add_to_registry: ClassVar[bool] = True

        # Last one wins (this is current behavior)
        assert FirstItem.registry["duplicate"] == SecondItem
        assert FirstItem.registry["duplicate"] != first_class

    def test_no_key_attribute_defined(self) -> None:
        """
        Test behavior when registry_key is defined but corresponding
        class attribute is missing
        """

        with pytest.raises(RegistryKeyAttributeNotDefined):

            class MissingKeyAttribute(  # pyright: ignore[reportUnusedClass]
                AutoRegistry
            ):
                """
                Class with registry_key missing
                """

                add_to_registry: ClassVar[bool] = True

    def test_no_key_value_defined(self) -> None:
        """
        Test behavior when registry_key is defined but value is None or empty
        """

        with pytest.raises(RegistryKeyIsNoneError):

            class BaseRegisteredItem(  # pyright: ignore[reportUnusedClass]
                AutoRegistry
            ):
                """
                Class with registry_key value set to None for testing
                """

                registry_key: ClassVar[str] = "key_attr"
                add_to_registry: ClassVar[bool] = False

            class NoKeyValueItem(  # pyright: ignore[reportUnusedClass]
                BaseRegisteredItem
            ):
                """
                Class with registry_key value set to None for testing
                """

                key_attr: str = ""
                add_to_registry: ClassVar[bool] = True

    def test_no_subclasses_registered(self) -> None:
        """
        Test behavior when no subclasses are registered
        """

        class BaseNoRegisteredItems(AutoRegistry):
            """
            Base class for testing with no registered subclasses
            """

            registry_key: ClassVar[str] = "test_type"
            add_to_registry: ClassVar[bool] = False

        class NoRegisteredItems(BaseNoRegisteredItems):
            """
            Class with no registered subclasses
            """

            test_type: Literal["none"] = "none"
            add_to_registry: ClassVar[bool] = False

        with pytest.raises(NoSubclassesRegisteredError):
            init_registry(NoRegisteredItems, "test_group")

    def test_one_subclass_registered(self) -> None:
        """
        Test behavior when only one subclass is registered
        """

        class BaseWithOneItem(AutoRegistry):
            """
            Base class for testing with only one registered subclass
            """

            registry_key: ClassVar[str] = "test_type"
            add_to_registry: ClassVar[bool] = False

        class OnlyOneItem(  # pyright: ignore[reportUnusedClass]
            BaseWithOneItem
        ):
            """
            Class with only one registered subclass
            """

            test_type: Literal["only"] = "only"
            add_to_registry: ClassVar[bool] = True

        init_registry(BaseWithOneItem, "test_group")

        assert len(BaseWithOneItem.registry) == 1
