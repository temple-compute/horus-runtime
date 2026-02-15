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
Implementation of the AutoRegistry class, which provides a mechanism for
automatically registering subclasses. This allows SDK users to define
new module types (such as artifacts) without needing to manually add the
new type to a central registry.
"""

from importlib.metadata import entry_points
from inspect import isabstract
from typing import Annotated, Any, ClassVar, Dict, Type, TypeVar, Union

from pydantic import Field

from horus_runtime.i18n import tr as _


class RegistryError(Exception):
    """
    Base exception for registry-related errors.
    """


class RegistryKeyAttributeNotDefined(RegistryError):
    """
    Exception raised when a subclass is missing a required registry key.
    """


class RegistryKeyIsNoneError(RegistryError):
    """
    Exception raised when a subclass has a registry key set to None.
    """


class NoSubclassesRegisteredError(RegistryError):
    """
    Exception raised when no subclasses are registered for a given base class.
    """


class AutoRegistry:
    """
    Base class for automatically registering subclasses.
    """

    registry: ClassVar[Dict[str, Type[Any]]]
    """
    A class variable that holds the registry of subclasses.
    """

    registry_key: ClassVar[str]
    """
    A class variable that defines the key used to register the subclass in the
    registry. Subclasses must define this variable to be registered in the
    registry. The value of this variable is used as the key in the registry
    to look up the subclass when instantiating an object from the registry.
    """

    add_to_registry: ClassVar[bool] = True
    """
    A class variable that indicates whether the subclass should be added to
    the registry. This can be set to False for subclasses that should not be
    registered in the registry, such as abstract base classes.
    """

    # This method is called when a subclass is defined. This allows us to look
    # up the correct subclass based on the 'registry_key' field when we want to
    # materialize an instance from the registry.
    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        # Initialize the registry if it doesn't exist yet. initializing it here
        # ensures that each subclass has its own registry, since the registry
        # is a class variable. If we initialized the registry in the base
        # class, all subclasses would share the same registry, which is not
        # what we want.
        if not hasattr(cls, "registry"):
            cls.registry = {}

        # Prevent abstract base classes from being registered in the
        # registry, since they should not be instantiated directly.
        # Skip registration if 'add_to_registry' is set to False.
        if isabstract(cls) or not cls.add_to_registry:
            return

        # If the subclass does not define a 'registry_key' class variable,
        # raise an error. It's not possible to use a subclass without a
        # 'registry_key' field, since the 'registry_key' field is used for
        # discriminating between different types in the instantiation process.

        # First we check if the 'registry_key' attribute is defined in the
        # first "base" class that has it defined inheriting from AutoRegistry
        has_key = hasattr(cls, "registry_key")

        if not has_key:
            raise RegistryKeyAttributeNotDefined(
                _(
                    "%(cls)s must define a class property named 'registry_key'"
                    " with a non-empty value to allow the auto-registration"
                    " mechanism to register subclasses in the corresponding"
                    " registry."
                )
                % {"cls": cls.__name__}
            )

        # Then we check if the value of the 'registry_key' attribute is not
        # None or empty
        key_value = getattr(cls, cls.registry_key, None)
        if not key_value:
            raise RegistryKeyIsNoneError(
                _(
                    "%(cls)s must define a class property named '%(key)s' with"
                    " a non-empty value to be registered in the corresponding"
                    " registry."
                )
                % {"cls": cls.__name__, "key": cls.registry_key}
            )

        # Register the subclass in the registry using the value of the
        # 'registry_key' field as the key.
        cls.registry[key_value] = cls


T = TypeVar("T", bound=AutoRegistry)


def init_registry(
    base_cls: type[T],
    entry_point_group: str,
) -> Any:
    """
    Generic function to build a Union type for all registered subclasses
    of a given base class.
    """

    print(
        _("Initializing %(group)s registry for %(cls)s")
        % {"group": entry_point_group, "cls": base_cls.__name__}
    )

    # Import plugins from metadata
    entries = entry_points(group=entry_point_group)
    for ep in entries:
        print(_(f"- {ep.value}"))
        ep.load()

    # If the registry is empty, raise an error
    # Hours could not work properly without implementations
    if not base_cls.registry:
        raise NoSubclassesRegisteredError(
            _(
                "No subclasses registered for %(cls)s. Ensure that there"
                " are subclasses of this base class with add_to_registry=True."
            )
            % {"cls": base_cls.__name__}
        )

    # If there is only one registered subclass, return it directly instead of
    # a Union
    def build_registry_union(
        base_cls: type[T],
    ):
        if len(base_cls.registry) == 1:
            return Annotated[
                next(iter(base_cls.registry.values())),
                Field(discriminator=base_cls.registry_key),
            ]

        return Annotated[
            Union[tuple(base_cls.registry.values())],
            Field(discriminator=base_cls.registry_key),
        ]

    return build_registry_union(base_cls)
