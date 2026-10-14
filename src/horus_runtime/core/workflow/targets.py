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
Resolution of a workflow's named targets (``targets:``) and the
``{ref: <name>}`` task targets that point at them.

Scoping is lexical: a subworkflow body sees its own ``targets:`` plus every
enclosing workflow's, inner names shadowing outer ones. Resolution happens on
the raw document of the outermost workflow, so a body's refs to a parent
target become the parent's very instance before the body is validated.
"""

import re
from collections.abc import Mapping
from typing import Any

from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.target.ref import TARGET_NAME_PATTERN, ref_name
from horus_runtime.core.workflow.exceptions import (
    InvalidTargetNameError,
    TargetIsReferenceError,
    TargetRefInLoopError,
    UnknownTargetRefError,
)


def _build_targets(raw: object) -> dict[str, BaseTarget]:
    """Validate a ``targets:`` map into named target instances."""
    if not isinstance(raw, Mapping):
        return {}
    built: dict[str, BaseTarget] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not re.fullmatch(
            TARGET_NAME_PATTERN, name
        ):
            raise InvalidTargetNameError(str(name))
        if ref_name(value) is not None:
            raise TargetIsReferenceError(name)
        target = (
            value
            if isinstance(value, BaseTarget)
            else BaseTarget.model_validate(value)
        )
        target._ref = name  # noqa: SLF001
        built[name] = target
    return built


def _has_ref(entry: object) -> bool:
    """Whether a raw task dict, or any task of a body it carries, has a ref."""
    if not isinstance(entry, dict):
        return False
    if ref_name(entry.get("target")) is not None:
        return True
    nested = [entry.get("body_template")]
    for key in ("body", "sub"):
        body = entry.get(key)
        if isinstance(body, dict) and isinstance(body.get("tasks"), list):
            nested += body["tasks"]
    if isinstance(entry.get("loop"), dict):
        nested.append(entry["loop"].get("body"))
    return any(_has_ref(task) for task in nested)


def _resolve_entry(entry: object, scope: Mapping[str, BaseTarget]) -> object:
    """One task entry with its ``{ref}`` (and any nested body's) resolved."""
    if not isinstance(entry, dict):
        # An already-built task carries a real target instance.
        return entry
    name = ref_name(entry.get("target"))
    if name is not None:
        if name not in scope:
            raise UnknownTargetRefError(str(entry.get("id")), name)
        entry = {**entry, "target": scope[name]}
    if _has_ref(entry.get("body_template")):
        raise TargetRefInLoopError
    body = entry.get("body")
    if entry.get("kind") == "subworkflow" and isinstance(body, dict):
        entry = {**entry, "body": resolve_target_refs(body, scope)}
    # A nested `sub:` is not lowered yet: resolve its body the same way.
    sub = entry.get("sub")
    if isinstance(sub, dict):
        entry = {**entry, "sub": resolve_target_refs(sub, scope)}
    return entry


def resolve_target_refs(
    data: dict[str, Any], scope: Mapping[str, BaseTarget] | None = None
) -> dict[str, Any]:
    """
    Return *data* (a raw workflow document) with its ``targets:`` built into
    named instances and every task's ``{ref: <name>}`` replaced by the
    instance it names, looked up in its own ``targets:`` first and then in
    *scope* (the enclosing workflows' targets).

    Raises:
        InvalidTargetNameError: For a ``targets:`` key that is not a name.
        TargetIsReferenceError: For a ``targets:`` entry that is a ``{ref}``.
        UnknownTargetRefError: For a ``{ref}`` naming no target in scope.
        TargetRefInLoopError: For a ``{ref}`` inside a loop body.
    """
    own = _build_targets(data.get("targets"))
    inner_scope = {**(scope or {}), **own}
    resolved = {**data, "targets": own} if "targets" in data else {**data}
    tasks = data.get("tasks")
    if isinstance(tasks, list):
        resolved["tasks"] = [
            _resolve_entry(entry, inner_scope) for entry in tasks
        ]
    return resolved
