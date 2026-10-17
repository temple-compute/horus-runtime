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

import asyncio
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, final

from horus_runtime.logging import horus_logger
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

# Transfers running right now, so a second consumer of the same artifact on
# the same machine waits for the first copy instead of racing it. Entries live
# only while their transfer runs.
_in_flight: dict[tuple[object, ...], asyncio.Future[Path]] = {}


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
        *,
        skip_if_present: bool = False,
    ) -> None:
        """
        Transfer *artifact* from *source* target to *destination* target.

        When both targets share a filesystem (equal ``location_id``) there is
        nothing to move: the artifact is already reachable on the destination,
        so it is repointed at its on-target path and no strategy runs. This
        same-filesystem shortcut lives here, at the single entry point every
        transfer goes through, so no individual strategy has to re-implement
        it.

        ``skip_if_present`` asks the strategy to check whether the artifact
        already exists on the destination and, if so, to skip the transfer
        entirely (repointing the artifact at the existing on-target path).
        The check uses the destination target's own primitives
        (``path_exists`` + ``path_on_target``), so it works uniformly across
        every target kind.

        ``skip_if_present`` is deliberately *opt-in* and defaults to off: an
        existence check is only safe when the caller can prove the existing
        copy is not stale (e.g. a cache restore guarded by an input
        fingerprint). Skipping a transfer because a *stale* file happens to
        sit at the destination path would silently hand a consumer outdated
        bytes, so ordinary input materialization must keep transferring.

        Concurrent transfers of the same source path to the same destination
        machine (and generation) run once: later callers wait for the first
        and are repointed at its result. If that copy fails, each waiter runs
        its own.
        """
        if source.location_id == destination.location_id:
            artifact.path = Path(destination.path_on_target(artifact))
            return

        if skip_if_present:
            on_target = destination.path_on_target(artifact)
            if await destination.path_exists(on_target):
                artifact.path = Path(on_target)
                horus_logger.log.debug(
                    f"Artifact '{artifact.id}' already present on "
                    f"{destination.kind} target '{destination.location_id}' "
                    f"at {on_target}; skipping transfer"
                )
                return

        key = (
            asyncio.get_running_loop(),
            source.location_id,
            source.path_on_target(artifact),
            destination.location_id,
            destination.generation,
            destination.working_directory,
        )
        while (pending := _in_flight.get(key)) is not None:
            try:
                artifact.path = await asyncio.shield(pending)
                return
            except asyncio.CancelledError:
                # The copy we waited on failed: run our own unless we were
                # the one cancelled.
                task = asyncio.current_task()
                if task is None or task.cancelling():
                    raise

        pending = asyncio.get_running_loop().create_future()
        _in_flight[key] = pending
        try:
            await TransferMiddleware.call_with_middleware(
                TransferMiddlewareContext(
                    transfer_strategy=self,
                    artifact=artifact,
                    source=source,
                    destination=destination,
                ),
                lambda: self._transfer(artifact, source, destination),
            )
            pending.set_result(artifact.path)
        finally:
            pending.cancel()
            del _in_flight[key]

    @abstractmethod
    async def _transfer(
        self,
        artifact: "BaseArtifact",
        source: S,
        destination: D,
    ) -> None:
        """
        Implement the transfer of *artifact* from *source* to *destination*.

        **Contract — artifact.path after transfer:**

        After a successful ``_transfer``, ``artifact.path`` must be set to
        ``Path(destination.path_on_target(artifact))``.  This is the canonical
        path at which downstream code will look for the artifact on the
        *destination* target.  Every concrete strategy must uphold this
        invariant so that callers never have to guess which convention a
        particular strategy used.

        This is called by the public ``transfer`` method, which handles
        common pre- or post-transfer logic (same-filesystem shortcut,
        middleware, etc.).
        """
