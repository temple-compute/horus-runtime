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
    ArtifactItem,
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

    async def items(self, target: BaseTarget) -> list[ArtifactItem]:
        """
        One item per element of the JSON list this artifact holds, read
        strictly through *target*'s channels (never the local filesystem, so
        a remote target's file is fetched over the channel); each slot is a
        zero-padded index into the list.

        Raises:
            ArtifactIterationError: When the target-side document is not
                valid JSON or does not hold a JSON list.
        """
        raw = await target.get_file(target.path_on_target(self))
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactIterationError(
                _(
                    "JSON artifact '%(id)s' at %(path)s is not valid JSON: "
                    "%(err)s"
                )
                % {
                    "id": self.id,
                    "path": target.path_on_target(self),
                    "err": exc,
                }
            ) from exc

        if not isinstance(value, list):
            raise ArtifactIterationError(
                _(
                    "JSON artifact '%(id)s' must hold a JSON list to be "
                    "iterable; got %(type)s."
                )
                % {"id": self.id, "type": type(value).__name__}
            )

        width = max(1, len(str(max(len(value) - 1, 0))))
        return [
            ArtifactItem(slot=f"{i:0{width}d}", value=element)
            for i, element in enumerate(value)
        ]

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
