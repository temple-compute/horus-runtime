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
Target-agnostic transfer strategy.

Moves an artifact between any two targets using only the shared filesystem
primitives every target implements: the source packages the artifact where it
lives, the bytes flow through the orchestrator via ``get_file`` / ``put_file``,
and the destination unpackages it in place. It is used as the fallback when no
location-specific strategy is registered for a ``(source, destination)`` pair.
"""

from pathlib import Path, PurePosixPath

from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.store import ArtifactStore
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.transfer.strategy import BaseTransferStrategy
from horus_runtime.logging import horus_logger


class GenericTransfer(BaseTransferStrategy):
    """
    Fallback transfer that works over any pair of targets.

    Not registered by key (``add_to_registry = False``); the orchestrator uses
    it directly when :meth:`BaseTransferStrategy.get_from_registry` finds no
    specific strategy, so location-specific strategies always take precedence.
    """

    add_to_registry = False

    async def _transfer(
        self,
        artifact: BaseArtifact,
        source: BaseTarget,
        destination: BaseTarget,
    ) -> None:
        """
        Transfer *artifact* from *source* to *destination*.

        Packages on the source, streams the single package file through the
        orchestrator, and unpackages on the destination. The same-filesystem
        case (equal ``location_id``) is handled upstream by
        :meth:`BaseTransferStrategy.transfer` and never reaches here.

        **Path consistency (Bug #71):** ``dest_path`` is derived from
        ``destination.resolved_working_directory`` — the same source used by
        executors to compute ``task.working_dir``.  This eliminates any
        working-directory mismatch between the transfer and the executor, so
        output files are always found by direct path lookup with no ``find``
        fallback required.
        """
        src_store = ArtifactStore(source)
        dst_store = ArtifactStore(destination)

        src_path = source.path_on_target(artifact)
        pkg_src = await src_store.package(artifact)

        data = await source.get_file(pkg_src)

        name = PurePosixPath(pkg_src).name
        pkg_dst = f"{destination.resolved_working_directory}/{name}"
        await destination.put_file(data, pkg_dst)

        # Derive dest_path from destination.resolved_working_directory so it
        # is consistent with how executors locate files (task.working_dir =
        # target.resolved_working_directory / task.id).  The previous approach
        # used destination.path_on_target(artifact) which, for targets that do
        # not override path_on_target, returns the source's absolute path —
        # causing a working-dir mismatch that forced a slow `find` fallback.
        dest_path = (
            f"{destination.resolved_working_directory}"
            f"/{PurePosixPath(artifact.path).name}"
        )

        # Repoint the artifact at the destination path *before* unpackage so
        # that path_on_target (used internally by dst_store.unpackage) returns
        # the correct destination location.
        artifact.path = Path(dest_path)

        await dst_store.unpackage(artifact, pkg_dst)

        # Best-effort cleanup of staging packages. Never remove a package that
        # *is* the real artifact: identity packaging leaves pkg_src == the
        # source file, and for a single file pkg_dst == the destination file.
        if pkg_src != src_path:
            await self._safe_remove(source, pkg_src)
        if pkg_dst != dest_path:
            await self._safe_remove(destination, pkg_dst)

    @staticmethod
    async def _safe_remove(target: BaseTarget, path: str) -> None:
        """Remove *path* on *target*, logging (not raising) on failure."""
        try:
            await target.remove(path)
        except Exception as exc:
            horus_logger.log.debug(
                "Failed to clean up staging package '%s' on %s: %s",
                path,
                target.kind,
                exc,
            )
