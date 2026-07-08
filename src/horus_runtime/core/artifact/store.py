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
Mediator between artifacts and the filesystem where they physically live.

An artifact's existence and lifecycle depend on *where* it materializes, which
is owned by the target. The :class:`ArtifactStore` binds an artifact to a
target and performs those operations through the target's artifact-agnostic
filesystem primitives, so neither
:class:`~horus_runtime.core.artifact.base.BaseArtifact` nor
:class:`~horus_runtime.core.target.base.BaseTarget` has to carry the
cross-cutting logic.
"""

import shlex
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.artifact.base import BaseArtifact
    from horus_runtime.core.target.channel import ChannelProcess


@runtime_checkable
class TargetFilesystem(Protocol):
    """
    The minimal filesystem surface an :class:`ArtifactStore` needs from a
    target. :class:`~horus_runtime.core.target.base.BaseTarget` satisfies this
    structurally.
    """

    @property
    def resolved_working_directory(self) -> str:
        """Base directory on the target for per-run generated files."""
        ...

    def path_on_target(self, artifact: "BaseArtifact") -> str:
        """Absolute path where *artifact* lives on the target's filesystem."""
        ...

    async def path_exists(self, path: str) -> bool:
        """Whether *path* exists on the target's filesystem."""
        ...

    async def remove(self, path: str) -> None:
        """Remove *path* (file or directory) on the target's filesystem."""
        ...

    async def run_command_sync(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> "ChannelProcess":
        """Run *cmd* synchronously on the target over a live channel."""
        ...


class ArtifactStore:
    """
    Performs artifact lifecycle operations against the filesystem of a specific
    target.
    """

    def __init__(self, target: TargetFilesystem) -> None:
        self.target = target

    async def exists(self, artifact: "BaseArtifact") -> bool:
        """
        Check whether *artifact* has materialized on the target.
        """
        return await self.target.path_exists(
            self.target.path_on_target(artifact)
        )

    async def delete(self, artifact: "BaseArtifact") -> None:
        """
        Delete *artifact* from the target, emitting the standard delete event
        when it existed.
        """
        path = self.target.path_on_target(artifact)
        if not await self.target.path_exists(path):
            return

        await self.target.remove(path)
        artifact.emit_event(ArtifactEventsEnum.DELETE)

    async def package(self, artifact: "BaseArtifact") -> str:
        """
        Produce a single transferable file representing *artifact* on the
        target and return its path.

        Single-file artifacts (``pack_command`` returns ``None``) are their
        own package, so the artifact's own path is returned untouched.
        Otherwise the artifact's pack command runs on the target to build a
        package under the target's working directory.
        """
        src = self.target.path_on_target(artifact)
        pkg = self._package_path(src)
        cmd = artifact.pack_command(src, pkg)
        if cmd is None:
            return src

        await self._run(cmd, artifact, "package")
        artifact.emit_event(ArtifactEventsEnum.PACKAGE)
        return pkg

    async def unpackage(
        self, artifact: "BaseArtifact", package_path: str
    ) -> None:
        """
        Materialize *artifact* on the target from the single-file package at
        *package_path*.

        Single-file artifacts (``unpack_command`` returns ``None``) are moved
        into place; others run their unpack command on the target.
        """
        dest = self.target.path_on_target(artifact)
        cmd = artifact.unpack_command(package_path, dest)
        if cmd is None:
            if package_path != dest:
                parent = str(PurePosixPath(dest).parent)
                cmd = (
                    f"mkdir -p {shlex.quote(parent)} && "
                    f"mv -f {shlex.quote(package_path)} {shlex.quote(dest)}"
                )
                await self._run(cmd, artifact, "unpackage")
        else:
            await self._run(cmd, artifact, "unpackage")

        artifact.emit_event(ArtifactEventsEnum.UNPACKAGE)

    def _package_path(self, src: str) -> str:
        """
        Path for a generated package on the target, kept distinct from the
        artifact's own path so packaging never clobbers the source.
        """
        name = PurePosixPath(src).name
        base = self.target.resolved_working_directory
        return f"{base}/{name}.horuspkg"

    async def _run(self, cmd: str, artifact: "BaseArtifact", op: str) -> None:
        """
        Run *cmd* on the target and raise when it exits non-zero.
        """
        proc = await self.target.run_command_sync(cmd)
        rc = await proc.wait()
        if rc != 0:
            raise RuntimeError(
                _(
                    "Failed to %(op)s artifact '%(id)s' on target "
                    "(exit code %(rc)s)"
                )
                % {"op": op, "id": artifact.id, "rc": rc}
            )
