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

    kind_name: ClassVar[str] = "BaseArtifact"
    """Human-readable name for this artifact kind."""

    kind_description: ClassVar[str] = ""
    """Description of this artifact kind."""

    id: str
    """
    The artifact's stable ID.
    """

    name: str = ""
    """
    Human-readable display label for this artifact (shown on the node handles).
    Defaults to ``id`` when omitted.
    """

    description: str = ""
    """
    Optional free-text description of the artifact, shown in the UI.
    """

    path: Annotated[Path, BeforeValidator(validate_path)]
    """
    Absolute local filesystem path where the artifact materializes.
    """

    declared_path: Path | None = Field(default=None, init=False, exclude=True)
    """
    The path exactly as declared (before resolution), preserved so a workflow
    can re-anchor relative artifact paths to its run directory. ``None`` until
    :meth:`resolve_path` runs. See ``BaseWorkflow._resolve_run_paths``.
    """

    kind: str
    """
    Type of the artifact, such as 'file', 'folder', 'dataset', 'model', etc.

    Subclasses of Artifact must set this field to a specific value to uniquely
    identify the type of artifact. This field is used for discriminating
    between different types of artifacts when they are stored in the artifact
    registry.
    """

    @model_validator(mode="after")
    def resolve_path(self) -> Self:
        """
        Normalize artifact paths to absolute resolved paths.

        The declared (pre-resolution) path is preserved on ``declared_path``
        so a workflow can re-anchor a relative path to its run directory; the
        eager CWD resolution here remains the default for standalone use.
        """
        self.declared_path = self.path
        self.path = self.path.resolve()
        return self

    @model_validator(mode="after")
    def default_name(self) -> Self:
        """
        Fall back to ``id`` as the display name when none is provided.
        """
        if not self.name:
            self.name = self.id

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

    def pack_command(self, src: str, pkg: str) -> str | None:
        """
        Return a portable shell command that produces the single-file package
        *pkg* from the artifact materialized at *src*, or ``None`` when the
        artifact is already a single file (identity packaging).

        The command is executed by
        :class:`~horus_runtime.core.artifact.store.ArtifactStore` on the target
        where the artifact lives, so it must be POSIX-portable and reference
        only *src* and *pkg*. Both are target-side paths.
        """
        del src, pkg
        return None

    def unpack_command(self, pkg: str, dest: str) -> str | None:
        """
        Return a portable shell command that materializes the artifact at
        *dest* from the single-file package *pkg*, or ``None`` when the
        artifact is a single file (identity: the store moves *pkg* into place).

        Executed by the :class:`.ArtifactStore` on the destination target; must
        be POSIX-portable and reference only *pkg* and *dest*.
        """
        del pkg, dest
        return None

    def emit_event(self, event_name: ArtifactEventsEnum) -> None:
        """
        Emit the standard artifact event. Public entry point used by
        :class:`~horus_runtime.core.artifact.store.ArtifactStore` after it
        performs a filesystem operation (delete, package, unpackage) on behalf
        of the artifact.
        """
        self._emit_event(event_name)

    def _emit_event(self, event_name: ArtifactEventsEnum) -> None:
        """
        Emit the standard artifact event.
        """
        HorusContext.get_context().bus.emit(
            ArtifactEvent(
                message=_("Artifact at %(path)s. %(event)s")
                % {"path": self.path, "event": event_name.name},
                artifact_id=str(self.id),
                event_name=event_name,
            )
        )
