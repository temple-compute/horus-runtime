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
Defines the Executor base class, which represents an executor in the Horus
runtime. An executor is on charge of actually running the task, by using the
specified runtime in a certain environment, for example running it locally as
a command or running it inside a SLURM job, either remote or locally.
"""

import tempfile
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, final

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.middleware.executor import (
    ExecutorMiddleware,
    ExecutorMiddlewareContext,
)
from horus_runtime.registry.auto_registry import AutoRegistry
from horus_runtime.settings import runtime_settings

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask

RuntimeFilterType = tuple[type[BaseRuntime], ...]


def _is_safe_component(name: str) -> bool:
    """
    True if *name* is a single, safe path component.

    Side-artifact entry names come from a target's ``list_dir`` (possibly a
    remote, untrusted channel), so reject path separators and parent refs to
    prevent traversal when building local paths.
    """
    return (
        name not in ("", ".", "..")
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
    )


class BaseExecutor(AutoRegistry, entry_point="executor"):
    """
    The base executor represents the abstract concept of an executor in the
    Horus runtime. An executor is on charge of actually running the task in the
    designated runtime and environment.
    """

    registry_key: ClassVar[str] = "kind"

    kind: str
    """
    The 'kind' field is used to identify the specific type of executor.
    """

    kind_name: ClassVar[str] = "BaseExecutor"
    """
    Human-readable name for this executor type, used in the UI.
    """

    kind_description: ClassVar[str] = _("Horus base executor")
    """
    Description of this executor type, used in the UI.
    """

    runtimes: ClassVar[RuntimeFilterType] = (BaseRuntime,)
    """
    Which runtime types this executor can handle. By default, an executor can
    handle any runtime type.
    """

    @final
    async def execute(self, task: "BaseTask") -> None:
        """
        Execute the task using the specified runtime and environment. This
        method is final and should not be overridden by subclasses. Instead,
        subclasses should implement the `_execute` method, which contains the
        specific execution logic for different types of executors.
        """
        # Create the side-artifacts directory through the channel so the same
        # code works on both local and remote targets (M2.3).
        await task.target.mkdir(task.side_artifacts_dir)

        try:
            await ExecutorMiddleware.call_with_middleware(
                ExecutorMiddlewareContext(executor=self, task=task),
                lambda: self._execute(task),
            )
        finally:
            # Collect side artifacts after execution.
            await self.collect_side_artifacts(task)

    @abstractmethod
    async def _execute(self, task: "BaseTask") -> None:
        """
        Execute the task using the specified runtime and environment.
        This method should be implemented by subclasses to define the specific
        execution logic for different types of executors.
        """

    async def collect_side_artifacts(self, task: "BaseTask") -> None:
        """
        Collect side artifacts produced during task execution and bring them
        back to the orchestrator's local filesystem.

        Side artifacts live in ``task.side_artifacts_dir`` on the **target**
        host, which is not necessarily the orchestrator's filesystem. They are
        listed and transferred over the target channel (``list_dir`` +
        ``get_file``) into a local temporary directory, then registered as
        :class:`FileArtifact` (top-level files) or :class:`FolderArtifact`
        (top-level folders, with their nested contents reconstructed locally).

        Side artifacts are meant to be small, inspectable outputs (logs,
        plots, small intermediates). Files larger than
        ``runtime_settings.MAX_SIDE_ARTIFACT_BYTES`` are skipped with a
        warning; large data should be declared as task inputs/outputs, which
        have their own transfer strategies.
        """
        try:
            entries = await task.target.list_dir(task.side_artifacts_dir)
        except Exception as exc:
            horus_logger.log.warning(
                _(
                    "Failed to list side artifacts for task "
                    "%(task_id)s: %(err)s"
                )
                % {"task_id": task.id, "err": exc}
            )
            return

        if not entries:
            return

        cap = runtime_settings.MAX_SIDE_ARTIFACT_BYTES
        safe_id = "".join(
            c if (c.isalnum() or c in "-_.") else "_" for c in task.id
        )
        landing = Path(tempfile.mkdtemp(prefix=f"horus-side-{safe_id}-"))

        for entry in entries:
            try:
                if not _is_safe_component(entry.name):
                    horus_logger.log.warning(
                        _("Skipping side artifact with unsafe name %(name)s")
                        % {"name": entry.name}
                    )
                    continue
                if entry.is_dir:
                    local_path = await self._pull_tree(
                        task, entry.path, landing / entry.name, cap
                    )
                    task.side_artifacts.append(
                        FolderArtifact(
                            id=f"{task.id}_{entry.name}", path=local_path
                        )
                    )
                else:
                    if entry.size > cap:
                        horus_logger.log.warning(
                            _(
                                "Skipping large side artifact %(name)s "
                                "(%(size)d bytes)"
                            )
                            % {"name": entry.name, "size": entry.size}
                        )
                        continue
                    local_path = landing / entry.name
                    local_path.write_bytes(
                        await task.target.get_file(entry.path)
                    )
                    task.side_artifacts.append(
                        FileArtifact(
                            id=f"{task.id}_{entry.name}", path=local_path
                        )
                    )
            except Exception as exc:
                horus_logger.log.warning(
                    _("Failed to collect side artifact %(name)s: %(err)s")
                    % {"name": entry.name, "err": exc}
                )

    async def _pull_tree(
        self,
        task: "BaseTask",
        remote_root: str,
        local_root: Path,
        cap: int,
    ) -> Path:
        """
        Reconstruct the target directory tree rooted at *remote_root* under
        *local_root*, using the channel (``list_dir`` + ``get_file``).

        Walks iteratively (no recursion-depth limit). Every directory is
        created locally so empty directories are preserved; files larger than
        *cap* are skipped with a warning. Returns *local_root*.
        """
        stack: list[tuple[str, Path]] = [(remote_root, local_root)]
        while stack:
            remote_dir, local_dir = stack.pop()
            local_dir.mkdir(parents=True, exist_ok=True)
            for child in await task.target.list_dir(remote_dir):
                if not _is_safe_component(child.name):
                    horus_logger.log.warning(
                        _("Skipping side artifact with unsafe name %(name)s")
                        % {"name": child.name}
                    )
                    continue
                local_child = local_dir / child.name
                if child.is_dir:
                    stack.append((child.path, local_child))
                elif child.size > cap:
                    horus_logger.log.warning(
                        _(
                            "Skipping large side artifact %(name)s "
                            "(%(size)d bytes)"
                        )
                        % {"name": child.name, "size": child.size}
                    )
                else:
                    local_child.write_bytes(
                        await task.target.get_file(child.path)
                    )
        return local_root
