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
Definitions of the artifact registry, which is responsible for managing the
artifacts in the Horus runtime. The artifact registry provides a way to
register, retrieve, and manage artifacts based on their unique identifiers,
paths, and types. It serves as a central repository for all artifacts in the
runtime, allowing for efficient storage and retrieval of artifacts based on
their metadata and content. The artifact registry can be implemented using
various storage backends, such as a local file system, a database, or a cloud
storage service, depending on the requirements of the runtime and the scale of
the artifacts being managed.
"""

from typing import Annotated, Any, TypeAlias, Union

from pydantic import Field

import horus_runtime.core.artifact as core_artifacts
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.registry.auto_registry import AutoRegistry
from horus_runtime.core.registry.registry_scan import scan_package

# Typed alias for better readability.
RegistryUnion: TypeAlias = Any


def init_registry(
    base_cls: type[AutoRegistry],
) -> RegistryUnion:
    """
    Generic function to build a Union type for all registered subclasses
    of a given base class.
    """

    # Scan the package containing the core artifacts to ensure that all core
    scan_package(core_artifacts)

    # Scan plugins...

    return Annotated[
        Union[tuple(base_cls.registry.values())],
        Field(discriminator=base_cls.registry_key),
    ]


ArtifactRegistry = init_registry(BaseArtifact)
