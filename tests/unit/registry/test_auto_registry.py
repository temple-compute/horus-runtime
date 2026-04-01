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
Unit tests for AutoRegistry class.
"""

from typing import ClassVar

import pytest

from horus_runtime.registry.auto_registry import AutoRegistry
from horus_runtime.registry.exceptions import (
    DuplicatedRegistryKeyError,
    RegistryKeyAttributeNotDefinedError,
    RegistryKeyIsNoneError,
)


class RegistryBaseTest(AutoRegistry, entry_point="test_registry"):
    """
    Test base class for AutoRegistry testing.
    """

    registry_key: ClassVar[str] = "test_type"
    add_to_registry: ClassVar[bool] = False  # Prevent pollution


class ConcreteRegistryItem(RegistryBaseTest):
    """
    Concrete implementation for testing.
    """

    test_type: str = "concrete"
    add_to_registry: ClassVar[bool] = True


class AnotherConcreteItem(RegistryBaseTest):
    """
    Another concrete implementation for testing.
    """

    test_type: str = "another"
    add_to_registry: ClassVar[bool] = True


class AbstractTestItem(RegistryBaseTest):
    """
    Abstract class that should not be registered.
    """

    test_type: str = "abstract"
    add_to_registry: ClassVar[bool] = False


@pytest.mark.unit
class TestAutoRegistry:
    """
    Test cases for AutoRegistry class.
    """

    def test_registry_initialization(self) -> None:
        """
        Test that registry is properly initialized for subclasses.
        """
        assert hasattr(RegistryBaseTest, "registry")
        assert isinstance(RegistryBaseTest.registry, dict)

    def test_concrete_items_registered(self) -> None:
        """
        Test that concrete items are registered when add_to_registry is True.
        """
        assert "concrete" in ConcreteRegistryItem.registry
        assert "another" in ConcreteRegistryItem.registry
        assert (
            ConcreteRegistryItem.registry["concrete"] == ConcreteRegistryItem
        )
        assert AnotherConcreteItem.registry["another"] == AnotherConcreteItem

    def test_abstract_items_not_registered(self) -> None:
        """
        Test that abstract items are not registered when add_to_registry is
        False.
        """
        # Should not be in registry
        assert "abstract" not in AbstractTestItem.registry

    def test_registry_separation_between_hierarchies(self) -> None:
        """
        Test that different inheritance hierarchies have separate registries.
        """

        class DifferentBase(AutoRegistry, entry_point="different"):
            """
            Different base class for testing separate registries.
            """

            registry_key: ClassVar[str] = "different_key"
            add_to_registry: ClassVar[bool] = False

        class DifferentConcrete(DifferentBase):
            """
            Test class.
            """

            different_key: str = "diff"
            add_to_registry: ClassVar[bool] = True

        # Should have separate registries
        assert hasattr(RegistryBaseTest, "registry")
        assert hasattr(DifferentBase, "registry")
        assert RegistryBaseTest.registry is not DifferentBase.registry  # type: ignore[comparison-overlap]
        # Intentional: verifying __init_subclass__ created separate registry
        # dicts per base class, not a shared one. Types differ by design.

    def test_registry_key_from_attribute(self) -> None:
        """
        Test that registry_key value is taken from the class attribute.
        """

        class TestKeyItem(AutoRegistry, entry_point="test_key"):
            """
            Test class for registry key from attribute.
            """

            registry_key: ClassVar[str] = "my_key"
            my_key: str = "test_value"
            add_to_registry: ClassVar[bool] = True

        assert "test_value" in TestKeyItem.registry
        assert TestKeyItem.registry["test_value"] == TestKeyItem

    def test_multiple_inheritance_registry(self) -> None:
        """
        Test registry behavior with multiple inheritance scenarios.
        """

        class MixinClass:
            """
            Mixin class for testing.
            """

            mixin_attr: str = "mixin"

        class MultiInheritItem(RegistryBaseTest, MixinClass):
            """
            Test class for multiple inheritance scenarios.
            """

            test_type: str = "multi"
            add_to_registry: ClassVar[bool] = True

        assert "multi" in MultiInheritItem.registry
        assert MultiInheritItem.registry["multi"] == MultiInheritItem

    def test_registry_overwrite_protection(self) -> None:
        """
        Test behavior when multiple classes try to register with same key.
        """

        class FirstItem(RegistryBaseTest):
            """
            First class to register.
            """

            test_type: str = "duplicate"
            add_to_registry: ClassVar[bool] = True

        with pytest.raises(DuplicatedRegistryKeyError):

            class SecondItem(RegistryBaseTest):
                """
                Second class to register.
                """

                test_type: str = "duplicate"
                add_to_registry: ClassVar[bool] = True

    def test_no_key_attribute_defined(self) -> None:
        """
        Test behavior when registry_key is defined but corresponding
        class attribute is missing.
        """
        with pytest.raises(RegistryKeyAttributeNotDefinedError):

            class MissingKeyAttribute(AutoRegistry, entry_point="missing_key"):
                """
                Class with registry_key missing.
                """

                add_to_registry: ClassVar[bool] = True

    def test_no_key_value_defined(self) -> None:
        """
        Test behavior when registry_key is defined but value is None or empty.
        """
        with pytest.raises(RegistryKeyIsNoneError):

            class BaseRegisteredItem(AutoRegistry, entry_point="base_item"):
                """
                Class with registry_key value set to None for testing.
                """

                registry_key: ClassVar[str] = "key_attr"
                add_to_registry: ClassVar[bool] = False

            class NoKeyValueItem(BaseRegisteredItem):
                """
                Class with registry_key value set to None for testing.
                """

                key_attr: str = ""
                add_to_registry: ClassVar[bool] = True

    def test_one_subclass_registered(self) -> None:
        """
        Test behavior when only one subclass is registered.
        """

        class BaseWithOneItem(AutoRegistry, entry_point="test_group"):
            """
            Base class for testing with only one registered subclass.
            """

            registry_key: ClassVar[str] = "test_type"
            add_to_registry: ClassVar[bool] = False

        class OnlyOneItem(BaseWithOneItem):
            """
            Class with only one registered subclass.
            """

            test_type: str = "only"
            add_to_registry: ClassVar[bool] = True

        AutoRegistry.init_registry([BaseWithOneItem])

        assert len(BaseWithOneItem.registry) == 1
