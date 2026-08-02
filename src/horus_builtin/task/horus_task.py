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
from pathlib import PurePosixPath
from typing import ClassVar

from pydantic import BaseModel

from horus_builtin.event.task_event import HorusTaskEvent
from horus_builtin.target.local import LocalTarget
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.exceptions import ArtifactDoesNotExistError
from horus_runtime.core.artifact.store import ArtifactStore
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.target.exceptions import WorkingDirectoryNotSetError
from horus_runtime.core.task.base import BaseTask
from horus_runtime.i18n import tr as _

_UNHASHABLE = "unhashable"
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

    target: BaseTarget = LocalTarget()
    """
    The default target for a HorusTask is LocalTarget (in-process).
    """

    async def _run(self) -> None:
        """
        For a HorusTask, nothing needs to be done here, as the command is
        already specified in the runtime and will be executed by the executor.
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

        # Execute the command using the executor
        await self.executor.execute(self)

        # Only a run that got this far may claim its outputs match its inputs.
        if fingerprint is not None:
            await self._write_manifest(fingerprint)

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

    async def _fingerprint(self) -> TaskFingerprint:
        """
        Everything that must stay unchanged for the previous run's outputs to
        still be valid: the digest of every input, plus a hash of the task's
        own configuration, so editing the command invalidates the cache too.
        """
        store = ArtifactStore(self.target)
        # ponytail: a folder input digests to None, so a task whose inputs are
        # all folders keeps the old existence-only behaviour, upgrade path is a
        # recursive digest in ArtifactStore.digest.
        inputs = {
            artifact.id: await store.digest(artifact) or _UNHASHABLE
            for artifact in self.inputs
        }
        config = json.dumps(
            {
                "runtime": self.runtime.model_dump(mode="json"),
                "executor": self.executor.model_dump(mode="json"),
            },
            sort_keys=True,
        )
        config_hash = hashlib.sha256(config.encode()).hexdigest()
        return TaskFingerprint(inputs=inputs, config_hash=config_hash)

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

    async def is_complete(self) -> bool:
        """
        A HorusTask is complete when all of its output artifacts exist *and*
        its inputs and configuration are the ones the recorded manifest was
        written for. Outputs left over from before manifests existed re-run
        once: we cannot prove their inputs are unchanged.
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
        return recorded is not None and recorded == await self._fingerprint()

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
        path = self._manifest_path()
        if path is not None:
            await self.target.remove(path)

        self.runs = 0
