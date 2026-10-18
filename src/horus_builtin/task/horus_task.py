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
Default Horus task implementation.
"""

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.target.local import LocalTarget
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.exceptions import ArtifactDoesNotExistError
from horus_runtime.core.artifact.store import ArtifactStore
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.target.exceptions import WorkingDirectoryNotSetError
from horus_runtime.core.target.ref import target_or_ref_schema
from horus_runtime.core.task.base import BaseTask, InputDigests
from horus_runtime.i18n import tr as _

_UNHASHABLE = "unhashable"


def _digest_file(path: Path) -> str:
    """
    sha256 of a local file, read in chunks.
    """
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


"""Fingerprint value for an input the target cannot digest."""


class TaskFingerprint(BaseModel):
    """
    Everything that must stay unchanged for a task's recorded outputs to
    still be valid.
    """

    inputs: dict[str, str]
    config_hash: str


class HorusTask(BaseTask):
    """
    The HorusTask represents a basic task in the Horus runtime.
    """

    kind: str = "horus_task"
    kind_name: ClassVar[str] = "Horus Task"
    kind_description: ClassVar[str] = _("Basic Horus task")

    target: BaseTarget = Field(
        default=LocalTarget(), json_schema_extra=target_or_ref_schema
    )
    """
    The default target for a HorusTask is LocalTarget (in-process).
    """

    async def _run(self) -> None:
        """
        Validate inputs, fingerprint them, delegate the actual work to
        :meth:`_execute`, then record the fingerprint once it succeeds.
        """
        ctx = HorusContext.get_context()

        ctx.bus.emit(
            HorusTaskEvent(
                task_id=self.id,
                task_name=self.name,
                message=_("Task %(task_name)s started.")
                % {"task_name": self.name},
            )
        )

        self.runs += 1

        store = ArtifactStore(self.target)

        # Gather inputs
        for artifact in self.inputs:
            if not await store.exists(artifact):
                raise ArtifactDoesNotExistError(
                    _("Input artifact '%(input_id)s' does not exist")
                    % {"input_id": artifact.id}
                )

        # A task without outputs never reports complete, so it has nothing to
        # memoize. Taken before execution because the executor rewrites the
        # runtime as it runs, while the next run's check starts from the
        # un-rewritten state.
        fingerprint = await self._fingerprint() if self.outputs else None

        await self._execute()

        # Only a run that got this far may claim its outputs match its inputs.
        if fingerprint is not None:
            await self._write_manifest(fingerprint)

    async def _execute(self) -> None:
        """
        The task's actual work: dispatch the runtime through the executor.
        """
        await self.executor.execute(self)

    def _manifest_path(self) -> str | None:
        """
        Where this task's input fingerprint is recorded on the target, or
        ``None`` when the target has no working directory to write it to (in
        which case the task simply never skips).
        """
        try:
            base = self.target.resolved_working_directory
        except WorkingDirectoryNotSetError:
            return None
        return f"{base}/.horus/{self.id}.json"

    async def _fingerprint(
        self, input_digests: dict[str, str | None] | None = None
    ) -> TaskFingerprint:
        """
        Everything that must stay unchanged for the previous run's outputs to
        still be valid: the digest of every input, plus a hash of the task's
        own configuration, so editing the command invalidates the cache too.

        Inputs are digested where they are staged on the task's target,
        unless *input_digests* already holds them (digested at their source,
        before any transfer: a digest does not depend on the path).
        """
        if input_digests is None:
            store = ArtifactStore(self.target)
            input_digests = {
                artifact.id: await store.digest(artifact)
                for artifact in self.fingerprinted_inputs
            }
        inputs = {
            artifact.id: input_digests.get(artifact.id) or _UNHASHABLE
            for artifact in self.fingerprinted_inputs
        }
        payload: dict[str, Any] = {
            "runtime": self.runtime.model_dump(mode="json"),
            "executor": self.executor.model_dump(mode="json"),
        }
        # A runtime holds its script as a path, so the dump above changes
        # when the path changes but not when the file does. Without this a
        # task keeps skipping after its code was edited.
        code = self._local_file_digests()
        if code:
            payload["code"] = code
        config = json.dumps(payload, sort_keys=True)
        config_hash = hashlib.sha256(config.encode()).hexdigest()
        return TaskFingerprint(inputs=inputs, config_hash=config_hash)

    def _local_file_digests(self) -> list[list[str]]:
        """
        Sorted ``[name, sha256]`` for the runtime's and executor's local
        files, so the same set hashes the same way twice.

        Keyed by name, not path, to add no new path dependence. A missing
        file is skipped: it drops out of the set, so the task re-runs.
        """
        digests: list[list[str]] = []
        for path in [
            *self.runtime.local_files(),
            *self.executor.local_files(),
        ]:
            try:
                digests.append([path.name, _digest_file(path)])
            except OSError:
                continue
        return sorted(digests)

    async def _write_manifest(self, fingerprint: TaskFingerprint) -> None:
        """
        Record *fingerprint* on the target, next to the outputs the run just
        produced.
        """
        path = self._manifest_path()
        if path is None:
            return

        await self.target.mkdir(str(PurePosixPath(path).parent))
        body = fingerprint.model_dump_json().encode()
        await self.target.put_file(body, path)

    async def _remove_manifest(self) -> None:
        """
        Remove the recorded fingerprint, so the next run executes the task.
        """
        path = self._manifest_path()
        if path is not None:
            await self.target.remove(path)

    async def _read_manifest(self) -> TaskFingerprint | None:
        """
        The fingerprint recorded by the last successful run, or ``None`` when
        there is none or it cannot be trusted. A broken manifest must mean
        "run the task", never a crashed workflow, so every read error maps to
        ``None``.
        """
        path = self._manifest_path()
        if path is None:
            return None

        try:
            return TaskFingerprint.model_validate_json(
                await self.target.get_file(path)
            )
        except Exception:
            return None

    async def is_complete(
        self, input_digests: InputDigests | None = None
    ) -> bool:
        """
        A HorusTask is complete when all of its output artifacts exist *and*
        its inputs and configuration are the ones the recorded manifest was
        written for. Outputs left over from before manifests existed re-run
        once: we cannot prove their inputs are unchanged.

        *input_digests*, when given, supplies the input digests in place of
        the staged inputs' (see :meth:`_fingerprint`). An input it cannot
        digest makes the task incomplete: it would only match a manifest
        that could not hash it either.
        """
        # If no outputs are declared, we consider the task incomplete and
        # always run it
        if not self.outputs:
            return False

        store = ArtifactStore(self.target)
        for artifact in self.outputs:
            if not await store.exists(artifact):
                return False

        recorded = await self._read_manifest()
        if recorded is None:
            return False

        digests = None
        if input_digests is not None:
            digests = await input_digests()
            if None in digests.values():
                return False
        return recorded == await self._fingerprint(digests)

    async def is_complete_before_transfer(
        self, input_digests: InputDigests
    ) -> bool:
        """
        :meth:`is_complete` from the inputs' digests at their source.
        """
        return await self.is_complete(input_digests)

    async def manifest_matches(
        self, manifest: bytes, input_digests: InputDigests
    ) -> bool:
        """
        Whether *manifest* (the bytes of a ``.horus/<id>.json``) is the one
        :meth:`is_complete` would accept with *input_digests*, without
        reading the outputs or the manifest on the target.
        """
        if not self.outputs:
            return False
        try:
            recorded = TaskFingerprint.model_validate_json(manifest)
        except ValueError:
            return False
        digests = await input_digests()
        if None in digests.values():
            return False
        return recorded == await self._fingerprint(digests)

    async def _reset(self) -> None:
        """
        Reset the task by deleting all output artifacts. This allows the task
        to be re-run from scratch.
        """
        ctx = HorusContext.get_context()

        ctx.bus.emit(
            HorusTaskEvent(
                message=_("Resetting task %(task_name)s.")
                % {"task_name": self.name},
                task_id=self.id,
                task_name=self.name,
            )
        )

        store = ArtifactStore(self.target)
        for artifact in self.outputs:
            await store.delete(artifact)

        # The manifest only describes those outputs, so it goes with them.
        await self._remove_manifest()

        self.runs = 0
