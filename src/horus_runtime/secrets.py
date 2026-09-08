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
Mark, find, and redact credential fields on plugin models.

A plugin model has no way to say "this field is a secret" today -- a
password is just a ``str``, indistinguishable from a hostname, so nothing
that serializes a workflow (``to_yaml``, ``package_workflow``, tc-os's export
routes) can single it out. :class:`Secret` is that declaration: a plugin
opts a field in with a one-word change, ``password: Secret | None = None``.

:class:`Secret` already inherits ``pydantic.SecretStr``'s safe defaults --
``repr``/``str``/``model_dump(mode="json")`` show ``**********``, never the
value. That default is *masking*, which is fine for logs but the wrong
answer for an export a colleague might re-import: a masked value round-trips
back in as the literal string ``"**********"``. :func:`iter_secret_fields`
and :func:`redact` are the extra step ``to_yaml`` and ``package_workflow``
take before writing: replace the field with a ``${secret:<ref>}`` reference
instead, which is portable and inert. :meth:`Secret.resolve` resolves such a
reference lazily, from the run-time environment or a local secrets file,
never from the workflow document itself.
"""

import copy
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, SecretStr

#: Matches a secret reference literal, e.g. ``${secret:ssh-password}``.
_REF_RE = re.compile(r"^\$\{secret:([A-Za-z0-9_.:-]+)\}$")


class SecretResolutionError(RuntimeError):
    """No value is available for a secret reference at run time."""


def secret_ref(raw: str) -> str | None:
    """Return the reference id in *raw* (``${secret:<ref>}``), or ``None``."""
    match = _REF_RE.match(raw)
    return match.group(1) if match else None


def ref_for_path(path: str) -> str:
    """
    Derive a stable, template-safe reference id from a dotted field path.

    Used for a literal value with no reference of its own yet (see
    :func:`redact`), so redacting the same document twice yields the same
    ref rather than a fresh one each time.
    """
    return re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_")


def _env_key(ref: str) -> str:
    return "HORUS_SECRET_" + re.sub(r"[^A-Za-z0-9]+", "_", ref).upper()


def _resolve(ref: str) -> str:
    """
    Resolve *ref* to a value: the environment first, then a local secrets
    file, matching the export side's own precedence (an operator can always
    override a file-provisioned secret with an env var).
    """
    env_key = _env_key(ref)
    if env_key in os.environ:
        return os.environ[env_key]
    secrets_file = os.environ.get("HORUS_SECRETS_FILE")
    if secrets_file:
        data = yaml.safe_load(Path(secrets_file).read_text(encoding="utf-8"))
        if isinstance(data, dict) and ref in data:
            return str(data[ref])
    raise SecretResolutionError(
        f"No value for secret reference {ref!r}: set {_env_key(ref)} or add "
        f"it to the file named by $HORUS_SECRETS_FILE."
    )


class Secret(SecretStr):
    """
    A model field marker for a credential.

    Declare it in place of ``str`` to opt a plugin field into export
    redaction: ``password: Secret | None = None``. Everything else follows
    from ``SecretStr``: ``repr``/``str`` show ``**********``, and the JSON
    schema pydantic generates carries ``format: "password"`` (already what
    tc-os's frontend keys its password-input heuristic on) and
    ``writeOnly: True``.

    A value validated from a ``${secret:<ref>}`` literal is a *reference*:
    :attr:`ref` reports it, and :meth:`resolve` resolves it lazily from the
    environment or a local secrets file rather than returning the literal
    string. Any other value is a literal secret, ``ref is None``, and
    :meth:`resolve` returns it as-is -- the plugin using it (e.g.
    ``asyncssh.connect``) is none the wiser.

    ``get_secret_value()``, inherited unchanged from ``SecretStr``, is
    deliberately *not* overridden to resolve: pydantic's own default JSON
    serialization (the masking every other caller of ``model_dump`` relies
    on) calls it internally to build ``**********``, and that must keep
    working even when nothing has resolved the reference yet -- an
    unredacted ``model_dump(mode="json")`` of an already-exported workflow
    must not raise just because this process has no value for a secret it
    is about to overwrite with a reference anyway. Only :meth:`resolve` --
    called by a plugin at the point it actually needs the credential --
    performs the lookup, and only there can it fail.
    """

    def __init__(self, secret_value: str) -> None:
        super().__init__(secret_value)
        self._ref = secret_ref(secret_value)

    @property
    def ref(self) -> str | None:
        """The reference id, if this value is a ``${secret:<ref>}`` literal."""
        return self._ref

    def resolve(self) -> str:
        """The real value: as-is for a literal, looked up for a reference."""
        if self._ref is None:
            return self.get_secret_value()
        return _resolve(self._ref)


def iter_secret_fields(
    obj: Any, _prefix: str = ""
) -> Iterator[tuple[str, Secret]]:
    """
    Walk *obj* and yield ``(dotted_path, secret)`` for every :class:`Secret`
    field reachable from it.

    *obj* is normally a :class:`~horus_runtime.core.workflow.base.BaseWorkflow`
    instance, but the walk is generic over any pydantic model plus the
    lists and dicts a workflow is built from (``tasks``, nested
    ``target``/``runtime``/``executor``, artifact lists), so it finds a
    secret at any depth without a plugin registering anything.

    Paths use ``.`` throughout, including for list indices
    (``tasks.1.target.password``) rather than ``tasks[1]...``, so a caller
    holding the *dumped* dict (tc-os, which never imports horus-runtime's
    plugin models) can apply the same path to it with nothing more than
    ``str.split(".")``.
    """
    if isinstance(obj, Secret):
        if _prefix:
            yield _prefix, obj
        return
    if isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            value = getattr(obj, name, None)
            path = f"{_prefix}.{name}" if _prefix else name
            yield from iter_secret_fields(value, path)
        return
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from iter_secret_fields(item, f"{_prefix}.{i}")
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from iter_secret_fields(value, f"{_prefix}.{key}")
        return


def _set_path(data: Any, parts: list[str], value: str) -> None:
    node = data
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def redact(
    data: dict[str, Any], secret_fields: Iterable[tuple[str, Secret]]
) -> dict[str, Any]:
    """
    Return a deep copy of *data* with each secret-marked field replaced by a
    ``${secret:<ref>}`` reference -- never the value, not even masked.

    *data* is normally ``workflow.model_dump(mode="json")`` and
    *secret_fields* ``iter_secret_fields(workflow)`` on the same instance --
    kept separate rather than combined into one call so a caller that only
    has the dumped dict (tc-os) can pass paths it derived itself instead. A
    value that already came from a ``${secret:<ref>}`` literal keeps its own
    ref, so redacting an already-redacted document is a no-op; a literal
    value is assigned a ref derived from its path (see :func:`ref_for_path`).
    """
    out = copy.deepcopy(data)
    for path, secret in secret_fields:
        ref = secret.ref or ref_for_path(path)
        _set_path(out, path.split("."), f"${{secret:{ref}}}")
    return out
