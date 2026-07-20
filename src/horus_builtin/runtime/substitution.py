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
Shared artifact substitution helper for string-templating runtimes.

All runtimes that do string substitution (``command``, ``python_script``,
``python``) use a single entry point: :func:`substitute`.  Placeholders follow
``string.Template`` ``$``/``${}`` syntax — ``str.format`` ``{}`` is **not**
supported and passes through unchanged.

Supported placeholder forms
----------------------------
* ``$id``          — on-target path of the artifact whose id is *id*
* ``${id}``        — same, braced form (use when a letter/digit follows)
* ``${id.attr}``   — attribute of the artifact (e.g. ``${result.path}``,
                     ``${result.id}``)
* ``${task.attr}`` — attribute of the task (e.g. ``${task.name}``)

Unknown ``$name`` references are left as-is (``safe_substitute`` semantics).
Use ``$$`` to emit a literal ``$``.
"""

from collections.abc import Iterator, Mapping
from string import Template
from typing import TYPE_CHECKING

from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from pathlib import PurePath

    from horus_runtime.core.artifact.base import BaseArtifact
    from horus_runtime.core.target.base import BaseTarget
    from horus_runtime.core.task.base import BaseTask


# Wraps an artifact so that ``$id`` in a template formats to the artifact's
# path *on the task's target* (target.path_on_target), while ``${id.path}``,
# ``${id.id}``, etc. still forward to the artifact.  This is what lets a
# command or script be written once and run unchanged on a local or remote
# target.
class _ArtifactRef:
    def __init__(self, artifact: "BaseArtifact", target: "BaseTarget") -> None:
        self._a = artifact
        self._t = target

    def __getattr__(self, name: str) -> object:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._a, name)

    def __str__(self) -> str:
        return str(self._t.path_on_target(self._a))

    def __format__(self, spec: str) -> str:
        return format(self._t.path_on_target(self._a), spec)


# Create a namespace object to allow for attribute-style access to task
# variables in template placeholders.  This allows users to write placeholders
# like ``${task.name}`` in a workflow YAML.
class _TaskNamespace:
    def __init__(self, task: "BaseTask") -> None:
        for name, value in vars(task).items():
            setattr(self, name, value)


class _DotTemplate(Template):
    """Template subclass that allows dotted names like ``${result.path}``."""

    braceidpattern = r"(?a:[_a-z][_a-z0-9]*(?:\.[_a-z0-9]+)*)"


class _Resolver(Mapping[str, str]):
    """
    Mapping that resolves dotted placeholder keys against task artifacts and
    the task namespace itself.

    Keys are of the form ``id``, ``id.attr``, or ``task.attr``.  The root
    segments map to :class:`_ArtifactRef` objects (for artifact ids) and a
    :class:`_TaskNamespace` (for the ``task`` key).  Attribute chains are
    resolved via :func:`getattr`; :exc:`AttributeError` is converted to
    :exc:`KeyError` so ``safe_substitute`` leaves the placeholder intact.
    """

    def __init__(self, task: "BaseTask") -> None:
        self._roots: dict[str, object] = {
            a.id: _ArtifactRef(a, task.target)
            for a in (*task.inputs, *task.outputs)
        }
        self._roots["task"] = _TaskNamespace(task)

    def __getitem__(self, key: str) -> str:
        parts = key.split(".")
        if parts[0] not in self._roots:
            raise KeyError(key)
        obj: object = self._roots[parts[0]]
        for part in parts[1:]:
            try:
                obj = getattr(obj, part)
            except AttributeError as exc:
                raise KeyError(key) from exc
        return str(obj)

    def __iter__(self) -> "Iterator[str]":
        return iter(self._roots)

    def __len__(self) -> int:
        return len(self._roots)


def is_template(value: "str | PurePath") -> bool:
    """
    True if *value* carries a ``$`` placeholder and so must be rendered by
    :func:`substitute` against a task rather than used verbatim.

    Fields that normally hold a path on the orchestrator (a script, a conda
    environment file) accept ``${artifact_id}`` instead, naming an input
    artifact that the transfer layer has already placed on the target. That
    lets a workflow run on a machine that never had the original file.

    ``$$`` is the escape for a literal ``$`` and does not make a template.
    """
    return "$" in str(value).replace("$$", "")


def substitute(template: str, task: "BaseTask") -> str:
    """
    Render *template* against *task* using ``string.Template`` ``$``/``${}``
    syntax.

    * ``$id`` / ``${id}`` — on-target path of the artifact whose id is *id*
    * ``${id.attr}``       — attribute of that artifact
    * ``${task.attr}``     — attribute of the task
    * Unknown placeholders are left as-is (``safe_substitute`` semantics).
    * ``$$`` emits a literal ``$``.

    Raises :exc:`ValueError` if any artifact has the reserved id ``"task"``.
    """
    artifacts = (*task.inputs, *task.outputs)
    if any(a.id == "task" for a in artifacts):
        raise ValueError(
            _(
                "Artifact id 'task' is reserved for templates. "
                "Please rename this artifact."
            )
        )
    return _DotTemplate(template).safe_substitute(_Resolver(task))
