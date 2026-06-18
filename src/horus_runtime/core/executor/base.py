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

from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, final

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.i18n import tr as _
from horus_runtime.middleware.executor import (
    ExecutorMiddleware,
    ExecutorMiddlewareContext,
)
from horus_runtime.registry.auto_registry import AutoRegistry

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask

RuntimeFilterType = tuple[type[BaseRuntime], ...]


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
        Collect side artifacts produced during task execution.

        For ``LocalTarget`` (and any target whose ``side_artifacts_dir`` is
        accessible as a local path), this iterates the directory and registers
        every file and folder as a side artifact on the task.

        For remote targets the directory is not locally accessible, so this
        method is best-effort: it attempts a local ``Path`` walk and silently
        skips collection when the path does not exist locally.

        .. ponytail: full remote collection via channel ``ls`` + ``get_file``
           is the upgrade path (M2.3 follow-up); not needed for the local demo.
        """
        # TODO: Use the channel to list and retrieve side artifacts from
        # remote targets.
        local_path = Path(str(task.side_artifacts_dir))
        if not local_path.exists():
            return

        for artifact_path in local_path.iterdir():
            if artifact_path.is_file():
                task.side_artifacts.append(
                    FileArtifact(
                        id=f"{task.id}_{artifact_path.name}",
                        path=artifact_path,
                    )
                )
            elif artifact_path.is_dir():
                task.side_artifacts.append(
                    FolderArtifact(
                        id=f"{task.id}_{artifact_path.name}",
                        path=artifact_path,
                    )
                )
