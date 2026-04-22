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
Base transfer strategy. A transfer strategy knows how to move an artifact
between two targets with different location kinds.

Transfer strategies are auto-registered using the AutoRegistry pattern,
keyed by ``"source_kind.destination_kind"``. Concrete strategies declare
which target types they handle via ``handles_source`` and
``handles_destination`` class variables; the transfer key is derived
automatically from the ``kind`` defaults of those target types, producing
values such as ``"local.local"``.
"""

from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar, final

from horus_runtime.middleware.transfer import (
    TransferMiddleware,
    TransferMiddlewareContext,
)
from horus_runtime.registry.auto_registry import AutoRegistry
from horus_runtime.registry.auto_registry_product import AutoRegistryProduct

if TYPE_CHECKING:
    from horus_runtime.core.artifact.base import BaseArtifact
    from horus_runtime.core.target.base import BaseTarget

# Type aliases for strategy class attributes.
HandlesSourceType = type["BaseTarget"]
HandlesDestinationType = type["BaseTarget"]


class BaseTransferStrategy[
    S: BaseTarget = BaseTarget,
    D: BaseTarget = BaseTarget,
](AutoRegistryProduct, AutoRegistry, entry_point="transfer"):
    """
    Maps a (source target kind, destination target kind) pair to a concrete
    transfer implementation.
    """

    registry_key: ClassVar[str] = (
        "transfer_key:handles_source.handles_destination"
    )
    transfer_key: str | None = None
    handles_source: ClassVar[HandlesSourceType]
    handles_destination: ClassVar[HandlesDestinationType]

    @final
    async def transfer(
        self,
        artifact: "BaseArtifact",
        source: S,
        destination: D,
    ) -> None:
        """
        Transfer *artifact* from *source* target to *destination* target.
        """
        await TransferMiddleware.call_with_middleware(
            TransferMiddlewareContext(
                transfer_strategy=self,
                artifact=artifact,
                source=source,
                destination=destination,
            ),
            lambda: self._transfer(artifact, source, destination),
        )

    @abstractmethod
    async def _transfer(
        self,
        artifact: "BaseArtifact",
        source: S,
        destination: D,
    ) -> None:
        """
        Override this method to implement the transfer logic. This is called by
        the public ``transfer`` method, which handles any common pre- or
        post-transfer logic (e.g. logging, events, etc.).
        """
