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
Mixin for registries whose discriminator key is composed from the default
values of other ``AutoRegistry`` types.
"""

from inspect import isabstract
from typing import ClassVar, Unpack, cast

from pydantic import ConfigDict
from pydantic.fields import FieldInfo

from horus_runtime.registry.auto_registry import AutoRegistry


class AutoRegistryProduct:
    """
    Mixin that derives a registry key by composing the discriminator defaults
    of other ``AutoRegistry`` types referenced as ClassVar attributes.

    ``registry_key`` format::

        "<field_name>:<attr1>.<attr2>"

    ``<field_name>`` is the Pydantic field that stores the derived key.
    ``<attr1>``, ``<attr2>`` … are ClassVar attributes whose types are other
    ``AutoRegistry`` subclasses; their discriminator field defaults are joined
    with ``:`` to form the registry key.

    Put this mixin before ``AutoRegistry`` in the base list so the derived key
    is committed to the class **before** ``super().__init_subclass__`` hands
    control to ``AutoRegistry``, which reads the key for registration.
    """

    _KEY_SEPARATOR: ClassVar[str] = ":"
    _ATTR_SEPARATOR: ClassVar[str] = "."

    def __init_subclass__(cls, **kwargs: Unpack[ConfigDict]) -> None:
        """
        Derive the registry key from the referenced attributes and commit it to
        the class before AutoRegistry's __init_subclass__ runs.
        """
        if not issubclass(cls, AutoRegistry):
            raise TypeError(
                f"{cls.__name__} uses AutoRegistryProduct but does not "
                "inherit from AutoRegistry."
            )

        # Abstract classes and opted-out classes are never registered as
        # concrete implementations.
        if isabstract(cls) or not cls.add_to_registry:
            # Proceed with normal AutoRegistry processing,
            # which will skip registration for this case, but delegate
            # other sublcass initialization processing (pydantic model setup,
            # etc).
            cast(AutoRegistry, super()).__init_subclass__(**kwargs)
            return

        if cls._KEY_SEPARATOR not in cls.registry_key:
            # Warn the user that AutoRegistryProduct is being used without a
            # composite registry_key, which is useless and likely a mistake.
            raise ValueError(
                f"{cls.__name__} uses AutoRegistryProduct but registry_key "
                "is not in the expected 'field_name:attr1' format."
            )

        field_name, raw_attrs = cls.registry_key.split(
            cls._KEY_SEPARATOR, maxsplit=1
        )

        parts: list[str] = []
        for attr in raw_attrs.split(cls._ATTR_SEPARATOR):
            # Resolve the ClassVar attribute to the referenced registry
            # type.
            attr_type: type[AutoRegistry] | None = getattr(cls, attr, None)

            if attr_type is None:
                raise ValueError(
                    f"{cls.__name__} registry_key references attribute "
                    f"'{attr}' which does not exist."
                )

            # Read the discriminator default from that type's model_fields.
            key_field = attr_type.registry_key
            field = cast(FieldInfo, attr_type.model_fields.get(key_field))
            value: object = field.default if field is not None else None
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"{cls.__name__} registry_key references attribute "
                    f"'{attr}' whose registry key is not a non-empty string."
                )

            parts.append(value)

        # All attrs resolved: commit the composed key to the class so
        # AutoRegistry reads it during super().__init_subclass__.
        setattr(cls, field_name, cls._KEY_SEPARATOR.join(parts))
        cls.registry_key = field_name

        # Proceed with normal AutoRegistry processing, which will read the
        # derived registry_key and register the class.
        cast(AutoRegistry, super()).__init_subclass__(**kwargs)
