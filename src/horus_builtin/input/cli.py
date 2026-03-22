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
CLI Input implementation for horus-runtime.
"""

from typing import Literal

from horus_runtime.input.base import BaseInput


class CLIInput(BaseInput):
    """
    CLI input implementation. Prompts the user for input using the built-in
    input() function.
    """

    kind: Literal["cli"] = "cli"

    def ask(
        self,
        prompt: str,
        *,
        default: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str | None:
        """
        Prompt the user for input using the built-in input() function and
        return their response.
        """
        if default is not None:
            prompt = f"{prompt} [{default}]: "

        value = input(prompt) or default

        return value
