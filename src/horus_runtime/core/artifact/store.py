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
filesystem primitives.
"""

import asyncio
import hashlib
import os
import re
import secrets
import shlex
from collections.abc import Awaitable, Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal

from horus_builtin.event.artifact_event import ArtifactEventsEnum
from horus_runtime.core.target.base import STDERR_TAIL, BaseTarget
from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.artifact.base import BaseArtifact

CacheMode = Literal["deep", "fast"]
"""How an input is fingerprinted: ``deep`` hashes every byte, ``fast`` hashes
path, size and whole-second mtime, plus the content of small files."""

CACHE_MODES: tuple[CacheMode, ...] = ("deep", "fast")
"""Every cache mode, in the order error messages list them."""

SMALL_FILE = 1 << 20
"""``fast`` hashes files with a size below this by content."""

_PREFIX: dict[CacheMode, str] = {"deep": "deep:", "fast": "fast:"}
_TREE_TAG = b"horus-tree\n"
_STAT_TAG = b"horus-stat\n"
_HEX = re.compile(rb"[0-9a-f]{64}")

_DIGEST_PRELUDE = r"""export LC_ALL=C
h=sha256sum; command -v sha256sum >/dev/null || h='shasum -a 256'
st='stat -L -c %s:%Y'; stat -L -c %s / >/dev/null 2>&1 || st='stat -L -f %z:%m'
export h st
z=; printf '' | $h -z >/dev/null 2>&1 && z=1
hashes() {
  if [ -n "$z" ]; then find . -type f "$@" -print0 | xargs -0 -r sh -c \
    'printf "horus-names\0%s\0" $#; printf "%s\0" "$@"; $h -z "$@"' sh
  else find . -type f "$@" -exec sh -c \
    'for f; do printf "%s  %s\0" "$($h < "$f" | cut -c1-64)" "$f"; done' \
    sh {} +
  fi
}
stats() {
  if find . -maxdepth 0 -printf '' 2>/dev/null; then
    find . -type f -printf '%s:%T@ %p\0'
  else find . -type f -exec sh -c \
    'for f; do printf "%s %s\0" "$($st "$f")" "$f"; done' sh {} +
  fi
}
"""
"""Shell helpers of the digest command: ``hashes`` prints ``<hex>  <path>``
records and ``stats`` prints ``<size>:<mtime> <path>`` records, each ending in
NUL, through GNU tools or a fork-per-file fallback (busybox, macOS). A file's
size and mtime come from ``find -printf`` too where it exists, since uutils
``stat`` rounds ``%Y`` where GNU truncates. With
``-z``, each batch of ``hashes`` first prints ``horus-names``, its count and
the names as ``find`` gave them: uutils ``sha256sum`` rewrites non-UTF-8
names in its output."""

_DIGEST_BODY: dict[CacheMode, str] = {
    "deep": r"""if [ -d "$p" ]; then
  cd "$p" || exit 1; printf 'horus-tree\0'; hashes
elif [ -f "$p" ]; then
  printf 'horus-file\0'; $h < "$p"
else exit 1; fi""",
    "fast": r"""if [ -d "$p" ]; then
  cd "$p" || exit 1; printf 'horus-tree\0'
  stats && printf 'horus-content\0' && hashes -size -1048576c
elif [ -f "$p" ]; then
  printf 'horus-file\0'
  s=$(find -L "$p" -maxdepth 0 -printf '%s:%T@' 2>/dev/null) && [ -n "$s" ] ||
    s=$($st "$p") || exit 1
  printf '%s\0' "$s"
  if [ "${s%%:*}" -lt 1048576 ]; then $h < "$p"; fi
else exit 1; fi""",
}
"""Per-mode part of the digest command, run on the path in ``$p``."""


def digest_mode(digest: str) -> CacheMode:
    """The cache mode *digest* was taken in."""
    return "fast" if digest.startswith(_PREFIX["fast"]) else "deep"


def compose_folder_digest(
    mode: CacheMode, children: Mapping[str | bytes, str]
) -> str:
    """
    Digest of a folder whose entries digest to *children* (name -> digest),
    equal to :meth:`ArtifactStore.digest` of that folder. A child with the
    empty-folder digest is left out, so a subtree with no file never counts.
    """
    empty = _PREFIX[mode] + hashlib.sha256(_TREE_TAG).hexdigest()
    entries = sorted(
        (os.fsencode(name), digest)
        for name, digest in children.items()
        if digest != empty
    )
    body = b"".join(name + b"\0" + d.encode() + b"\0" for name, d in entries)
    return _PREFIX[mode] + hashlib.sha256(_TREE_TAG + body).hexdigest()


def _leaf(
    mode: CacheMode, size: int, mtime: int, content: str | None
) -> str | None:
    """A file's public digest from its size, whole-second mtime and hash."""
    if mode == "deep":
        return content
    if size < SMALL_FILE:
        return None if content is None else _PREFIX["fast"] + content
    stat = _STAT_TAG + b"%d:%d" % (size, mtime)
    return _PREFIX["fast"] + hashlib.sha256(stat).hexdigest()


_Tree = dict[bytes, "_Tree | str"]


def _tree_digest(mode: CacheMode, leaves: Mapping[bytes, str]) -> str:
    """Fold relative paths (``b"./a/b"``) -> leaf digest into folders."""
    tree: _Tree = {}
    for path, digest in leaves.items():
        *dirs, name = path.removeprefix(b"./").split(b"/")
        node = tree
        for part in dirs:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):  # changed while it was walked
                raise ValueError(path)
            node = child
        node[name] = digest

    def walk(node: _Tree) -> str:
        return compose_folder_digest(
            mode,
            {n: c if isinstance(c, str) else walk(c) for n, c in node.items()},
        )

    return walk(tree)


def _hex(record: bytes) -> str | None:
    """The sha256 hex at the start of a hasher's output record."""
    head = record[:64]
    return head.decode() if _HEX.fullmatch(head) else None


def _hash_record(record: bytes) -> str:
    """The hex of a ``<hex>  <path>`` record; :class:`ValueError` if not."""
    hexd = _hex(record)
    if hexd is None or record[64:66] not in (b"  ", b" *"):
        raise ValueError(record)
    return hexd


def _parse_digest(mode: CacheMode, out: bytes) -> str | None:
    """
    Public digest from the digest command's NUL-separated output. Raises
    :class:`ValueError` (or :class:`IndexError`) on a malformed record.
    """
    records = out.split(b"\0")
    if records[-1] == b"":
        records.pop()
    head, *rest = records
    if head == b"horus-file":
        if mode == "deep":
            (record,) = rest
            return _hex(record)
        size, mtime = rest[0].split(b":")
        (content,) = [_hex(r) for r in rest[1:]] or [None]
        return _leaf(mode, int(size), int(mtime.split(b".")[0]), content)
    if head != b"horus-tree":
        raise ValueError(head)

    stats: list[bytes] = []
    if mode == "fast":
        split = rest.index(b"horus-content")
        stats, rest = rest[:split], rest[split + 1 :]
    contents: dict[bytes, str] = {}
    i = 0
    while i < len(rest):
        if rest[i] != b"horus-names":
            contents[rest[i][66:]] = _hash_record(rest[i])
            i += 1
            continue
        n = int(rest[i + 1])
        names = rest[i + 2 : i + 2 + n]
        hashed = rest[i + 2 + n : i + 2 + 2 * n]
        if len(hashed) != n:  # a short batch also leaves names short
            raise ValueError(rest[i:])
        for name, record in zip(names, hashed, strict=True):
            contents[name] = _hash_record(record)
        i += 2 + 2 * n
    if mode == "deep":
        return _tree_digest(mode, contents)

    leaves: dict[bytes, str] = {}
    for record in stats:
        stat, path = record.split(b" ", 1)
        size, mtime = stat.split(b":")
        # %T@ carries fractions; whole seconds survive tar, scp and SFTP.
        leaf = _leaf(
            mode, int(size), int(mtime.split(b".")[0]), contents.get(path)
        )
        if leaf is None:
            return None
        leaves[path] = leaf
    return _tree_digest(mode, leaves)


async def shell_digest(
    target: BaseTarget, path: str, mode: CacheMode
) -> str | None:
    """
    Digest of *path* on *target* (see :meth:`ArtifactStore.digest`), taken in
    one command over the shell channel, like
    :meth:`~horus_runtime.core.target.base.BaseTarget.path_exists`, so it
    works for any target without a new primitive. ``shasum``, BSD ``stat``
    and a per-file loop are the fallbacks for macOS and busybox.
    """
    # ponytail: empty directories, symlinks inside a folder and file
    # modes are not part of a folder's digest, only regular files.
    script = f"p={shlex.quote(path)}\n{_DIGEST_PRELUDE}{_DIGEST_BODY[mode]}"
    proc = await target.run_command_sync(f"{{ {script}\n}} 2>/dev/null")
    out, _stderr = await proc.communicate()
    if await proc.wait() != 0:
        return None
    try:
        # Off the event loop: ~3-5 us per file, seconds at 1M files.
        return await asyncio.to_thread(_parse_digest, mode, out)
    except (ValueError, IndexError):
        return None


class ArtifactStore:
    """
    Performs artifact lifecycle operations against the filesystem of a specific
    target.
    """

    def __init__(self, target: BaseTarget) -> None:
        self.target = target

    async def exists(self, artifact: "BaseArtifact") -> bool:
        """
        Check whether *artifact* has materialized on the target.
        """
        return await self.target.path_exists(
            self.target.path_on_target(artifact)
        )

    async def digest(
        self, artifact: "BaseArtifact", mode: CacheMode = "deep"
    ) -> str | None:
        """
        Digest of *artifact* as it is on the target in cache *mode*, or
        ``None`` when it cannot be taken (missing, unreadable, no hashing
        tool, unexpected output).

        - ``deep``: a file digests to the sha256 of its bytes; a folder to
          ``deep:`` plus a Merkle hash of its regular files' contents and
          relative paths, so editing, adding, removing or renaming any file
          in it changes the digest.
        - ``fast``: ``fast:`` plus the same Merkle shape over each file's
          size and whole-second mtime, or its content when it is smaller than
          :data:`SMALL_FILE`, so big files are never read.

        A folder's digest equals :func:`compose_folder_digest` of its
        entries' digests, and never depends on its own path. Hashing happens
        target-side through
        :meth:`~horus_runtime.core.target.base.BaseTarget.path_digest`.
        """
        return await self.target.path_digest(
            self.target.path_on_target(artifact), mode
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
        Otherwise the package is built under the target's working directory
        by the target's ``pack_folder`` (``pack_format == "tar.gz"``) or the
        artifact's pack command.
        """
        src = self.target.path_on_target(artifact)
        pkg = self._package_path(src)
        if artifact.pack_format == "tar.gz":
            await self._hook(
                self.target.pack_folder(src, pkg), artifact, "package"
            )
        else:
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
        into place with the target's ``move_file``; folders (``pack_format ==
        "tar.gz"``) go through its ``unpack_folder``, others run their unpack
        command on the target.
        """
        dest = self.target.path_on_target(artifact)
        if artifact.pack_format == "tar.gz":
            await self._hook(
                self.target.unpack_folder(package_path, dest),
                artifact,
                "unpackage",
            )
        elif (cmd := artifact.unpack_command(package_path, dest)) is not None:
            await self._run(cmd, artifact, "unpackage")
        # Compare normalized paths: "wd//f" and "wd/f" are the same file
        # and ``mv`` refuses to move a file onto itself.
        elif PurePosixPath(package_path) != PurePosixPath(dest):
            await self._hook(
                self.target.move_file(package_path, dest),
                artifact,
                "unpackage",
            )

        artifact.emit_event(ArtifactEventsEnum.UNPACKAGE)

    def _package_path(self, src: str) -> str:
        """
        Path for a generated package on the target, kept distinct from the
        artifact's own path so packaging never clobbers the source. Unique per
        call, so two packages of same-named artifacts never share a file.
        """
        name = PurePosixPath(src).name
        base = PurePosixPath(self.target.resolved_working_directory)
        return str(base / f"{name}.{secrets.token_hex(4)}.horuspkg")

    async def _hook(
        self, call: Awaitable[None], artifact: "BaseArtifact", op: str
    ) -> None:
        """
        Await the target hook *call*, naming *artifact* when it fails.
        """
        try:
            await call
        except RuntimeError as exc:
            msg = _("Failed to %(op)s artifact '%(id)s' on target") % {
                "op": op,
                "id": artifact.id,
            }
            raise RuntimeError(f"{msg}: {exc}") from exc

    async def _run(self, cmd: str, artifact: "BaseArtifact", op: str) -> None:
        """
        Run *cmd* on the target and raise when it exits non-zero, including
        the tail of the command's stderr so the real cause is not lost.
        """
        proc = await self.target.run_command_sync(cmd)
        # communicate() drains both pipes, so large output cannot deadlock.
        _stdout, stderr = await proc.communicate()
        rc = await proc.wait()
        if rc != 0:
            msg = _(
                "Failed to %(op)s artifact '%(id)s' on target "
                "(exit code %(rc)s)"
            ) % {"op": op, "id": artifact.id, "rc": rc}
            tail = stderr.decode(errors="replace").strip()[-STDERR_TAIL:]
            raise RuntimeError(f"{msg}: {tail}" if tail else msg)
