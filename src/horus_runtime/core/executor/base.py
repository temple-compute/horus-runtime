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
        # The task's side-artifacts directory is created here so every executor
        # can rely on it existing without recreating it.
        task.side_artifacts_dir.mkdir(parents=True, exist_ok=True)

        await ExecutorMiddleware.call_with_middleware(
            ExecutorMiddlewareContext(executor=self, task=task),
            lambda: self._execute(task),
        )

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

        By default, it iterates the side-artifacts directory for any files
        produced and adds them as side-products to the task.
        """
        if not task.side_artifacts_dir.exists():
            return

        for artifact_path in task.side_artifacts_dir.iterdir():
            if artifact_path.is_file():
                task.side_artifacts.append(
                    FileArtifact(
                        id=artifact_path.stem, path=artifact_path, kind="file"
                    )
                )
            elif artifact_path.is_dir():
                task.side_artifacts.append(
                    FolderArtifact(
                        id=artifact_path.stem,
                        path=artifact_path,
                        kind="folder",
                    )
                )
