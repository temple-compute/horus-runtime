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
The iterable-artifact contract: artifacts that can enumerate themselves into
a deterministic list of ``(slot, item)`` pairs, reading strictly through a
target's channels (never assuming local filesystem access).

:class:`IterableArtifact` is a plain ABC mixin, deliberately *not* an
AutoRegistry root and not a kind of its own: concrete artifacts mix it in
next to their ``BaseArtifact`` base, and third-party plugins opt in exactly
the same way. Pydantic's ``ModelMetaclass`` derives from ``ABCMeta``, so the
multiple inheritance composes without extra plumbing.

Consumers:
- :mod:`horus_builtin.workflow.map` fans one clone out per item;
- the backend's kind listing derives an ``iterable`` trait from
  ``issubclass(cls, IterableArtifact)``.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from horus_runtime.core.target.base import BaseTarget


class ArtifactIterationError(Exception):
    """Raised when an artifact cannot be enumerated into items."""


@dataclass(frozen=True, slots=True)
class ArtifactItem:
    """
    One element of an iterable artifact, as produced by
    :meth:`IterableArtifact.items`.

    Exactly one of *path* / *value* is meaningful:

    - *path*: pre-materialized on-target path of the element; consumers
      repoint a copy at it directly (zero copy).
    - *value*: an in-memory Python value; the consumer serializes it through
      its own item template onto the target.
    """

    slot: str
    """Deterministic slot name (folder child name or zero-padded index)."""

    path: str | None = None
    """Pre-materialized on-target path: repoint at it, zero copy."""

    value: Any | None = None
    """Python value; the consumer's item template serializes it."""


class IterableArtifact(ABC):
    """
    Mixin for artifacts whose contents enumerate into a deterministic,
    ordered list of :class:`ArtifactItem` elements.
    """

    @abstractmethod
    async def items(self, target: "BaseTarget") -> list[ArtifactItem]:
        """
        Deterministic ``(slot, item)`` list, read via *target* channels only.

        Implementations must not touch the local filesystem: on a remote
        target the artifact lives on the target host, so every read has to go
        through ``target.list_dir``/``target.get_file`` against
        ``target.path_on_target(self)``.
        """
