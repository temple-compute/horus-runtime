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
from typing import ClassVar, Self, Unpack, cast

from pydantic import ConfigDict
from pydantic.fields import FieldInfo

from horus_runtime.registry.auto_registry import AutoRegistry


class AutoRegistryProduct:
    """
    Mixin that derives a registry key by composing the discriminator defaults
    of other ``AutoRegistry`` types referenced as ClassVar attributes.

    ``registry_key`` format (declared on the root/abstract class)::

        "<field_name>:<attr1>.<attr2>"

    ``<field_name>`` is the Pydantic field that stores the derived key.
    ``<attr1>``, ``<attr2>`` … are ClassVar attributes whose types are other
    ``AutoRegistry`` subclasses; their discriminator field defaults are joined
    with ``:`` to form the registry key.

    When a class explicitly declares this composite format, this mixin
    normalises ``registry_key`` to just ``<field_name>`` immediately (so that
    ``AutoRegistry`` always sees a plain field name as the discriminator) and
    stores the full template in ``_product_key_template``, which concrete
    subclasses inherit and use to drive composition.

    Put this mixin before ``AutoRegistry`` in the base list so the derived key
    is committed to the class **before** ``super().__init_subclass__`` hands
    control to ``AutoRegistry``, which reads the key for registration.
    """

    _KEY_SEPARATOR: ClassVar[str] = ":"
    _ATTR_SEPARATOR: ClassVar[str] = "."
    _product_key_template: ClassVar[str]

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

        # If this class explicitly declares a composite registry_key (one that
        # contains the separator), normalise it to just the field name so that
        # AutoRegistry always uses a plain field name as the discriminator.
        # The full template is preserved in _product_key_template so that
        # concrete subclasses can inherit and use it for composition.
        if "registry_key" in cls.__dict__:
            raw_key: str = cls.__dict__["registry_key"]
            if cls._KEY_SEPARATOR in raw_key:
                cls._product_key_template = raw_key
                cls.registry_key = raw_key.split(cls._KEY_SEPARATOR, 1)[0]

        # Abstract classes and opted-out classes are never registered as
        # concrete implementations.
        if isabstract(cls) or not cls.add_to_registry:
            # Proceed with normal AutoRegistry processing,
            # which will skip registration for this case, but delegate
            # other subclass initialization processing (pydantic model setup,
            # etc).
            cast(AutoRegistry, super()).__init_subclass__(**kwargs)
            return

        # Retrieve the composition template stored by the root/intermediate
        # class that declared the composite registry_key.
        template: str | None = getattr(cls, "_product_key_template", None)
        if not template or cls._KEY_SEPARATOR not in template:
            raise ValueError(
                f"{cls.__name__} uses AutoRegistryProduct but no composite "
                "registry_key template was found. Make sure a base class "
                "defines registry_key in 'field_name:attr1.attr2' format."
            )

        field_name, raw_attrs = template.split(cls._KEY_SEPARATOR, maxsplit=1)

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
        setattr(cls, field_name, cls._ATTR_SEPARATOR.join(parts))
        cls.registry_key = field_name

        # Proceed with normal AutoRegistry processing, which will read the
        # derived registry_key and register the class.
        cast(AutoRegistry, super()).__init_subclass__(**kwargs)

    @classmethod
    def get_from_registry(cls, *args: AutoRegistry) -> type[Self] | None:
        """
        Look up the matching class from the registry.

        Positional args map in order to the attrs declared in the
        ``registry_key`` template (``field:attr1.attr2``).  For each pair the
        discriminator value is read from the arg using the attr type's own
        ``registry_key``.
        """
        # We have to do some casting because AutoRegistryProduct is a mixin
        # and doesn't know the exact types of AutoRegistry. But because we
        # enforce the MRO, we are 100% sure that AutoRegistry methods are
        # always available.
        autoregistry_cls = cast(type[AutoRegistry], cls)

        parts: list[str] = []
        for arg in args:
            parts.append(getattr(arg, type(arg).registry_key))

        found_cls = autoregistry_cls.registry.get(
            cls._ATTR_SEPARATOR.join(parts)
        )

        # In order to return of type [self] and not [AutoRegistry], we have to
        # cast the result, but we are sure that if a class is returned
        # from the registry, it will be of the correct type.
        return cast(type[Self] | None, found_cls)
