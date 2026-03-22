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
Defines the BaseInput class, which represents an interactive input that can be
used in Horus workflows to ask users for information during execution.
"""

from abc import abstractmethod
from typing import Any, ClassVar

from horus_runtime.registry.auto_registry import AutoRegistry


class BaseInput(AutoRegistry, entry_point="input"):
    """
    Base class for workflow interactive inputs.

    The implementation must block until a value is available. horus_builtin
    provides a basic CLI input implementation.
    """

    registry_key: ClassVar[str] = "kind"
    kind: Any = ...

    @abstractmethod
    def ask(
        self,
        prompt: str,
        *,
        default: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """
        Ask the user for input with the given prompt and return their response.
        """
