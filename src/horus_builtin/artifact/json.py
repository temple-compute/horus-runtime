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
Implementation of the FolderArtifact class, which represents a local
folder/directory artifact in the Horus runtime.
"""

import json
from pathlib import Path
from typing import Any, ClassVar, cast

from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.iterable import (
    ArtifactIterationError,
    IterableArtifact,
)
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.i18n import tr as _


class JSONArtifact[T: Any = Any](BaseArtifact[T], IterableArtifact):
    """
    Represents a JSON-serializable Python object artifact.
    The artifact is materialized as a JSON file on disk.
    """

    kind: str = "json"
    kind_name: ClassVar[str] = "JSON"
    kind_description: ClassVar[str] = "A JSON-serializable data artifact."

    async def items(self, target: BaseTarget) -> list[BaseArtifact]:
        """
        One item per element of the JSON list this artifact holds: a new
        single-element ``JSONArtifact`` written into a ``<stem>.items``
        directory next to this one, so a consumer gets a real artifact it
        can pass to a task.

        Raises:
            ArtifactIterationError: When the target-side document is not
                valid JSON or does not hold a JSON list.
        """
        # TODO: Update read, write to support targets. Until then both the
        # read below and the per-item writes go through the local filesystem,
        # so iterating a JSON artifact only works on a co-located target.
        # https://github.com/temple-compute/horus-runtime/issues/174
        del target
        try:
            values = self.read()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactIterationError(
                _(
                    "JSON artifact '%(id)s' at %(path)s is not valid JSON: "
                    "%(err)s"
                )
                % {"id": self.id, "path": self.path, "err": exc}
            ) from exc

        if not isinstance(values, list):
            raise ArtifactIterationError(
                _(
                    "JSON artifact '%(id)s' must hold a JSON list to be "
                    "iterable; got %(type)s."
                )
                % {"id": self.id, "type": type(values).__name__}
            )

        # One sibling directory rather than N loose files: a large list would
        # otherwise bury the declared artifacts it sits next to.
        items_dir = self.path.with_name(f"{self.path.stem}.items")
        width = max(1, len(str(len(values) - 1)))
        artifacts: list[BaseArtifact] = []
        for index, element in enumerate(values):
            slot = f"{index:0{width}d}"
            item = JSONArtifact(
                id=f"{self.id}:{slot}", path=items_dir / f"{slot}.json"
            )
            item.write(element)
            artifacts.append(item)

        return artifacts

    def read(self) -> T:
        """
        Read and deserialize the JSON artifact contents.

        Warning: This method assumes that the JSON file is well-formed and that
        the contents can be deserialized into the expected type `T`. For more
        robust handling, consider using PydanticArtifact, which provides
        validation and error handling for deserialization.
        """
        with open(self.path) as f:
            j_contet = json.load(f)
        self._emit_event(ArtifactEventsEnum.READ)

        return cast(T, j_contet)

    def write(self, value: T) -> None:
        """
        Serialize and write the JSON artifact contents.

        Parent directories are created as needed so a task can write an output
        into a not-yet-existing results/ folder without a manual ``mkdir``.
        """
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(value, f)
        self._emit_event(ArtifactEventsEnum.WRITE)
