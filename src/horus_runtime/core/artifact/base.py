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
Defines the Artifact base class, which represents a local file-backed
artifact in the Horus runtime.
"""

import hashlib
import shutil
import uuid
from abc import abstractmethod
from pathlib import Path
from typing import Annotated, Any, ClassVar, Self

from pydantic import BeforeValidator, Field, model_validator

from horus_builtin.event.artifact_event import (
    ArtifactEvent,
    ArtifactEventsEnum,
)
from horus_runtime.context import HorusContext
from horus_runtime.i18n import tr as _
from horus_runtime.registry.auto_registry import AutoRegistry


def validate_path(path: Path | str) -> Path:
    """
    Allow artifact paths to be provided as either strings or Path objects, but
    normalize them to Path objects.
    """
    if isinstance(path, str):
        path = Path(path)
    return path


class BaseArtifact[T: Any = Any](AutoRegistry, entry_point="artifact"):
    """
    Represents a local file-backed artifact in the Horus runtime. An artifact
    is a piece of data that is produced or consumed by a task. It can be a
    file, a dataset, a model, a JSON file, pickled file or any other type of
    data that materializes on disk.

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

    internal_id: uuid.UUID = Field(
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

    id: str = ""
    """
    The artifact's user-friendly ID. This Id is used to sort task dependencies
    and should be unique within a workflow. If not provided, it will be set to
    the string representation of the internal_id.
    """

    @model_validator(mode="after")
    def set_id(self) -> Self:
        """
        If the user did not provide an 'id', set it to the string
        representation of 'internal_id'.
        """
        self.id = str(self.internal_id) if not self.id else self.id
        return self

    path: Annotated[Path, BeforeValidator(validate_path)]
    """
    Absolute local filesystem path where the artifact materializes.
    """

    kind: str
    """
    Type of the artifact, such as 'file', 'folder', 'dataset', 'model', etc.

    Subclasses of Artifact must set this field to a specific value to uniquely
    identify the type of artifact. This field is used for discriminating
    between different types of artifacts when they are stored in the artifact
    registry.
    """

    def exists(self) -> bool:
        """
        Checks if the artifact exists at the specified path.
        """
        return self.path.exists() and self.path.is_file()

    @property
    def hash(self) -> str | None:
        """
        Computes the hash of the file based on its contents. Returns None if
        the file does not exist.
        """
        return self.hash_file(self.path).hex() if self.exists() else None

    def delete(self) -> None:
        """
        Deletes the artifact from its location by deleting the file at the
        specified path.
        """
        if self.exists():
            self.path.unlink()
            self._emit_event(ArtifactEventsEnum.DELETE)

    @model_validator(mode="after")
    def resolve_path(self) -> Self:
        """
        Normalize artifact paths to absolute resolved paths.
        """
        self.path = self.path.resolve()
        return self

    @abstractmethod
    def read(self) -> T:
        """
        Read and deserialize the artifact contents.
        """

    @abstractmethod
    def write(self, value: T) -> None:
        """
        Write the native artifact representation to its canonical path.
        """

    def package(self) -> Path:
        """
        Return the single-file package that transports should move.
        """
        if not self.exists():
            raise FileNotFoundError(self.path)

        self._emit_event(ArtifactEventsEnum.PACKAGE)
        return self.path

    def unpackage(self, package_path: Path) -> None:
        """
        Materialize a packaged artifact file at the canonical path.
        """
        package_path = package_path.resolve()
        if package_path == self.path:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(package_path, self.path)
        self._emit_event(ArtifactEventsEnum.UNPACKAGE)

    def _emit_event(self, event_name: ArtifactEventsEnum) -> None:
        """
        Emit the standard artifact event.
        """
        HorusContext.get_context().bus.emit(
            ArtifactEvent(
                message=_("Artifact at %(path)s. %(event)s")
                % {"path": self.path, "event": event_name.name},
                artifact_id=str(self.internal_id),
                event_name=event_name,
            )
        )

    @staticmethod
    def hash_file(path: Path) -> bytes:
        """
        Computes the hash of the file contents.
        """
        buffer = 65536
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(buffer):
                sha256.update(chunk)

        return sha256.digest()
