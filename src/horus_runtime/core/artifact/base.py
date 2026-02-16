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
Defines the Artifact base class, which represents an artifact in the Horus
runtime. An artifact is a piece of data that is produced or consumed by a task.
"""

import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, model_validator

from horus_runtime.core.registry.auto_registry import AutoRegistry


class BaseArtifact(BaseModel, ABC, AutoRegistry):
    """
    Represents an artifact in the Horus runtime. An artifact is a piece of data
    that is produced or consumed by a task. It can be a file, a dataset,
    a model, a JSON file, pickled file or any other type of data.

    The artifact is identified by a unique ID.

    The workflow will derive status completion based on artifact existence
    and hash. If the artifact exists and the hash matches, the workflow
    considers the task that produces it as completed. If the artifact does
    not exist or the hash does not match, the workflow considers the task as
    not completed and will attempt to execute it to produce the artifact.


    In summary, the artifact is the fundamental unit of data in the Horus
    runtime, and main source of truth for workflow execution. The workflow
    relies on the existence and integrity of artifacts to determine the state
    of tasks and the overall workflow.
    """

    # The 'registry_key' class variable is used by the AutoRegistry base class
    # to determine how to register subclasses of Artifact in the registry.
    registry_key: ClassVar[str] = "kind"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        description=(
            "Unique identifier for the artifact. This can be generated using "
            "uuid.uuid4() when creating a new artifact."
        ),
    )
    """
    Unique identifier for the artifact. This can be generated using
    uuid.uuid4() when creating a new artifact.
    """

    uri: str
    """
    URI to the artifact. This can be a local path, a remote URL, or a
    reference to an artifact in a registry.
    """

    kind: Any | None = None
    """
    Type of the artifact, such as 'file', 'folder', 'dataset', 'model', etc.

    Subclasses of Artifact must set this field to a specific value to uniquely
    identify the type of artifact. This field is used for discriminating
    between different types of artifacts when they are stored in the artifact
    registry.
    """

    @abstractmethod
    def exists(self) -> bool:
        """
        Checks if the artifact exists at the specified path. This method should
        be implemented to check for the existence of the artifact based on its
        path.
        """

    @abstractmethod
    def materialize(self) -> Path:
        """
        Materializes the artifact to a local path and returns it.
        """

    @property
    @abstractmethod
    def hash(self) -> str | None:
        """
        Computes the hash of the artifact based on its content or returns
        None if the artifact does not exist.

        This method should be implemented to compute the hash based on the
        actual content of the artifact.
        """

    @model_validator(mode="after")
    def validate_kind(self) -> "BaseArtifact":
        """
        Validates that the kind field is set to a specific value in subclasses.
        This ensures that each subclass of Artifact has a unique kind value for
        proper discrimination in the artifact registry.
        """
        if self.kind is None:
            raise ValueError(
                "The 'kind' field must be set in subclasses of Artifact."
            )
        return self
