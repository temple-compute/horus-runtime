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
The ``{value, label}`` option shared by value artifacts that let the user
pick among authored alternatives.
"""

from pydantic import BaseModel

type OptionValue = str | int | float | bool
"""A JSON scalar: what the task's runtime receives for the option."""


class Option(BaseModel):
    """
    One authored alternative of a value artifact.

    ``value`` is what reaches the runtime; ``label`` is only what the UI shows,
    falling back to ``str(value)`` when it is ``None``.
    """

    value: OptionValue
    label: str | None = None
