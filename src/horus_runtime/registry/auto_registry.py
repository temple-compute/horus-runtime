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

from abc import ABC
from importlib.metadata import entry_points
from inspect import isabstract
from typing import Any, ClassVar

from pydantic import BaseModel, GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema
from typing_extensions import Self

from horus_runtime.i18n import tr as _
from horus_runtime.registry.exceptions import (
    BaseRegistryClassEntryPointNotDefinedError,
    DuplicatedRegistryKeyError,
    RegistryKeyAttributeNotDefinedError,
    RegistryKeyIsNoneError,
    RegistryPointExistsError,
)

HORUS_ENTRY_POINT_PREFIX: str = "horus."


class AutoRegistry(BaseModel, ABC):
    """
    Base class for automatically registering subclasses in a per-hierarchy
    registry. Subclasses are registered by their ``registry_key`` field value
    when they are defined, and can be looked up and instantiated by key at
    runtime.

    Usage
    -----
    Define a root registry class by passing ``entry_point="entry_point"``
    in the class definition. This marks the class as the top of a registry
    hierarchy and initialises an empty registry dict for it::

        class BaseArtifact(AutoRegistry, entry_point="artifact"):
            registry_key: ClassVar[str] = "type"
            type: str

    Concrete subclasses are then registered automatically when defined::

        class S3Artifact(BaseArtifact):
            type: str = "s3"
            bucket: str

    When a ``BaseArtifact`` field is used in a Pydantic model, incoming dicts
    are dispatched to the correct concrete class transparently::

        class MyWorkflow(BaseModel):
            artifact: BaseArtifact  # dispatches to S3Artifact, etc.
    """

    registry: ClassVar[dict[str, type[Self]]]
    """
    A class variable that holds the registry of concrete subclasses, keyed by
    the value of the field named by ``registry_key``. Each root registry class
    gets its own independent registry dict, initialised in
    ``__init_subclass__`` when ``entry_point="someting"``.
    """

    registry_key: ClassVar[str]
    """
    The name of the field whose value is used as the registry key when
    registering and looking up subclasses. Must be defined on every root
    registry class (e.g. ``registry_key = "type"``).
    """

    add_to_registry: ClassVar[bool] = True
    """
    Controls whether this class should be added to the registry when defined.
    Set to ``False`` on intermediate abstract base classes that should not be
    instantiated directly. Abstract classes (those with unimplemented abstract
    methods) are always excluded regardless of this flag.
    """

    _registry_roots: ClassVar[dict[type["AutoRegistry"], str]] = {}
    """
    Internal dict of classes that were declared with
    ``entry_point="something"``.
    The keys are the root classes, and the values are their corresponding
    entry point groups. Used by ``__get_pydantic_core_schema__`` to decide
    whether to intercept validation and dispatch to a concrete subclass, or
    to delegate to the default Pydantic schema generation for the class.
    """

    def __init_subclass__(
        cls: type[Self],
        entry_point: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Called automatically when a subclass is defined.

        If ``entry_point`` is provided, the class is marked as a dispatch
        root and given its own empty registry. Otherwise the subclass is
        validated and registered in the nearest root registry it belongs to.

        Parameters
        ----------
        entry_point:
            Pass a string when defining a new top-level registry hierarchy,
            this value will be used to load the entry point group for that
            hierarchy in ``init_registry()``, adding the horus. suffix (e.g.
            ``class BaseArtifact(AutoRegistry, entry_point="artifact")``).
            Artifact plugins will load then from the ``horus.artifact`` entry
            point group. Root classes are not registered in any registry
            themselves.
        """
        super().__init_subclass__(**kwargs)

        if entry_point:
            # Verify the point does not exist already
            if entry_point in AutoRegistry._registry_roots.values():
                raise RegistryPointExistsError(
                    _(
                        "%(entry_point)s already exists in the registry. "
                        "Entry points must be unique."
                    )
                    % {"entry_point": entry_point}
                )

            # Mark as a dispatch root and give it a fresh registry. Root
            # classes are not themselves registered as concrete
            # implementations.
            AutoRegistry._registry_roots[cls] = (
                HORUS_ENTRY_POINT_PREFIX + entry_point
            )
            cls.registry = {}

        # If the developer did NOT specify a entry_point, and the class
        # does NOT have a registry, means the dev forgot to add the
        # entry_point into this new registry class.
        if not entry_point and not hasattr(cls, "registry"):
            raise BaseRegistryClassEntryPointNotDefinedError(
                _(
                    "%(cls)s tried to register without specifying a "
                    "'entry_point'. Make sure all base classes that "
                    "inherit from AutoRegistry define an entry_point."
                )
                % {"cls": cls.__name__}
            )

        # Abstract classes and opted-out classes are never registered as
        # concrete implementations.
        if isabstract(cls) or not cls.add_to_registry:
            return

        # Every concrete subclass must declare a 'registry_key' class variable
        # so we know which field to read the discriminator value from.
        if not hasattr(cls, "registry_key"):
            raise RegistryKeyAttributeNotDefinedError(
                _(
                    "%(cls)s must define a class property named 'registry_key'"
                    " with a non-empty value to allow the auto-registration"
                    " mechanism to register subclasses in the corresponding"
                    " registry."
                )
                % {"cls": cls.__name__}
            )

        # The field named by 'registry_key' must carry a non-empty value on
        # the concrete class so the discriminator lookup works at runtime.
        key_value: str | None = getattr(cls, cls.registry_key, None)
        if not key_value:
            raise RegistryKeyIsNoneError(
                _(
                    "%(cls)s must define a class property named '%(key)s' with"
                    " a non-empty value to be registered in the corresponding"
                    " registry."
                )
                % {"cls": cls.__name__, "key": cls.registry_key}
            )

        # Check for duplicate registry keys to avoid silent overwriting of
        # existing entries.
        if key_value in cls.registry:
            raise DuplicatedRegistryKeyError(
                _(
                    "Duplicate registry key '%(key_value)s' "
                    "for %(cls)s and %(cls_key)s"
                )
                % {
                    "key_value": key_value,
                    "cls": cls.__name__,
                    "cls_key": cls.registry[key_value].__name__,
                }
            )

        # Register the concrete subclass under its discriminator value.
        cls.registry[key_value] = cls

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """
        Generate a custom Pydantic core schema for registry root classes.

        For root classes (those declared with ``registry_root=True``), this
        returns a plain validator that dispatches incoming dicts to the correct
        concrete subclass based on the discriminator field. For all other
        classes the default Pydantic schema generation is used.

        This hook is what allows a plain ``BaseArtifact`` field annotation to
        transparently deserialise into ``S3Artifact``, ``LocalArtifact``, etc.
        without any per-model boilerplate.
        """
        # Non-root classes (concrete subclasses) must use Pydantic's default
        # schema generation. If we intercepted here we would recurse infinitely
        # because dispatching calls back into validation.
        if cls not in cls._registry_roots:
            print(
                _("Generating default schema for %(cls)s")
                % {"cls": cls.__name__}
            )
            return handler(source_type)

        print(
            _("Generating dispatch schema for registry root %(cls)s")
            % {"cls": cls.__name__}
        )

        def validate(data: Any) -> Any:
            # Already a valid instance of this hierarchy, pass through.
            if isinstance(data, cls):
                return data

            if not isinstance(data, dict):
                raise TypeError(
                    f"Expected dict or {cls.__name__}, got {type(data)}"
                )

            discriminator = data.get(cls.registry_key)
            if not discriminator:
                raise ValueError(
                    f"Missing '{cls.registry_key}' discriminator in data"
                )

            target_cls = cls.registry.get(discriminator)
            if target_cls is None:
                raise ValueError(
                    f"Unknown {cls.registry_key}='{discriminator}' for"
                    f" {cls.__name__}. Registered: "
                    f"{tuple(cls.registry.keys())}"
                )

            # Use the pre-built validator on the concrete class directly.
            # Calling model_validate() here would re-enter
            # __get_pydantic_core_schema__ and cause infinite recursion.
            return target_cls.__pydantic_validator__.validate_python(data)

        return core_schema.no_info_plain_validator_function(validate)

    @staticmethod
    def init_registry(bases: list[type["AutoRegistry"]] | None = None) -> None:
        """
        Load all plugins registered under ``horus.*`` entry point groups.

        This method must be called once at application boot before any Pydantic
        model that contains a registry field is instantiated. Loading a plugin
        module causes its subclasses to be defined, which triggers
        ``__init_subclass__`` and populates the relevant registry.
        """
        # If a base list is provided, only load
        # the entry point groups for those bases.
        groups_to_load: set[str]
        if bases is not None:
            groups_to_load = {
                AutoRegistry._registry_roots[b]
                for b in bases
                if b in AutoRegistry._registry_roots
            }
        # Otherwise load all entry point groups for all registry roots.
        # This is the default behavior and ensures that all plugins are loaded.
        else:
            groups_to_load = {
                group
                for group in entry_points().groups
                if group.startswith(HORUS_ENTRY_POINT_PREFIX)
            }

        for group in groups_to_load:
            print(_("Initializing %(group)s registry.") % {"group": group})

            for horus_plugin in entry_points(group=group):
                print(
                    _("- %(entry_point)s")
                    % {"entry_point": horus_plugin.value}
                )

                # If a plugin fails to load, log the error and continue so
                # that a single broken plugin does not prevent the rest from
                # being registered.
                try:
                    horus_plugin.load()
                except Exception as e:
                    print(
                        _("Failed to load plugin %(entry_point)s: %(error)s")
                        % {"entry_point": horus_plugin.value, "error": str(e)}
                    )
