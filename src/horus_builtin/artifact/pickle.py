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
Implementation of PickleArtifact, which serializes arbitrary Python objects
to disk using the pickle protocol.
"""

import pickle
from typing import Any, cast

from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.core.artifact.base import BaseArtifact


class PickleArtifact[T: Any = Any](BaseArtifact[T]):
    """
    Represents a pickled Python object artifact.
    The artifact is materialized as a pickle file on disk.

    Warning: pickle is not secure against malformed or maliciously crafted
    data. Never unpickle data from untrusted sources.
    """

    kind: str = "pickle"

    def read(self) -> T:
        """
        Deserialize the artifact from the pickle file.
        """
        with open(self.path, "rb") as f:
            obj = pickle.load(f)
        self._emit_event(ArtifactEventsEnum.READ)
        return cast(T, obj)

    def write(self, value: T) -> None:
        """
        Serialize and write the object to the pickle file.
        """
        with open(self.path, "wb") as f:
            pickle.dump(value, f)
        self._emit_event(ArtifactEventsEnum.WRITE)
