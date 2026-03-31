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
Base runtime. The runtime represents the command, environment, and other
context in which a task is executed. The base runtime provides the foundational
functionality for executing tasks, and should be ingested by the executor.
"""

from abc import abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Self

from horus_runtime.registry.auto_registry import AutoRegistry

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class BaseRuntime[T: Any = Any](AutoRegistry, entry_point="runtime"):
    """
    The base runtime. This class provides the foundational functionality for
    executing tasks, and should be ingested by the executor.
    """

    registry_key: ClassVar[str] = "kind"

    kind: str
    """
    The 'kind' field is used to identify the specific type of runtime.
    """

    @abstractmethod
    def setup_runtime(self, task: "BaseTask[Self]") -> T:
        """
        Prepare the runtime to execute. This method should be implemented by
        subclasses to define the specific logic for preparing the command/task
        based on the runtime's context and environment.

        Returns:
            T: The prepared runtime instance.
        """
