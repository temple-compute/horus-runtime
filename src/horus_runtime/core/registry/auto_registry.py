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

from inspect import isabstract
from typing import Any, ClassVar, Dict, Type


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
        key_value = getattr(cls, cls.registry_key, None)
        if not key_value:
            raise ValueError(
                f"{cls.__name__} must have a value for the registry key "
                f"'{cls.registry_key}' to be registered in the registry"
            )

        # Register the subclass in the registry using the value of the
        # 'registry_key' field as the key.
        cls.registry[key_value] = cls
