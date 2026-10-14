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
A reference to a named target declared in a workflow's ``targets:`` map.

A task whose ``target`` is ``{ref: <name>}`` runs on the one shared
:class:`~horus_runtime.core.target.base.BaseTarget` instance declared under
that name, so every task with the same ref lands on the same machine.
"""

from pydantic import BaseModel, ConfigDict, Field

TARGET_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,62}$"
"""Allowed names in a workflow's ``targets:`` map."""


class TargetRef(BaseModel):
    """``{ref: <name>}``: the task runs on the workflow's target *name*."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=TARGET_NAME_PATTERN)
    """Name of an entry of the workflow's (or an enclosing) ``targets:``."""


def ref_name(value: object) -> str | None:
    """
    The name *value* refers to when it is a ``{ref: <name>}`` dict, else
    ``None``. A dict with ``ref`` and no ``kind`` is validated as a
    :class:`TargetRef`, so extra keys or a bad name raise.
    """
    if isinstance(value, dict) and "ref" in value and "kind" not in value:
        return TargetRef.model_validate(value).ref
    return None
