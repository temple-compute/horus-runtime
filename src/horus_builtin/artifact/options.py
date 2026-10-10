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
The ``{value, label}`` option shared by value artifacts that offer the user
a fixed set of choices (a boolean's two mapped states, a dropdown's entries).
"""

from pydantic import BaseModel


class Option(BaseModel):
    """
    One selectable state of a value artifact.

    ``value`` is what the task receives (``$id`` renders it); ``label`` is
    only what the UI shows, falling back to ``str(value)`` when unset.
    """

    value: str | int | float | bool
    label: str | None = None
