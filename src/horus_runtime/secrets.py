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

``Secret`` opts a field into export redaction (``password: Secret | None``).
Export replaces the value with ``${secret:<ref>}`` instead of pydantic's own
``**********`` mask, since a mask re-imports as a literal password. A JSON
dump (``model_dump(mode="json")``) keeps a reference as itself, so a dumped
workflow round-trips it; only a literal is masked.
``Secret.resolve()`` reads the real value back from the environment or a
local secrets file at run time, and refuses the mask.
"""

import copy
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, GetCoreSchemaHandler, SecretStr
from pydantic_core import SchemaSerializer, core_schema

__all__ = [
    "SECRET_MASK",
    "Secret",
    "SecretResolutionError",
    "env_key_for_ref",
    "iter_secret_fields",
    "iter_secret_refs",
    "redact",
    "ref_for_path",
    "secret_ref",
]

#: What pydantic dumps a masked ``SecretStr`` as. Never a real value: a
#: document holding it lost its secret to a masked dump.
SECRET_MASK = "**********"

#: Matches a secret reference literal, e.g. ``${secret:ssh-password}``.
_REF_RE = re.compile(r"^\$\{secret:([A-Za-z0-9_.:-]+)\}$")


class SecretResolutionError(RuntimeError):
    """No value is available for a secret reference at run time."""


def secret_ref(raw: str) -> str | None:
    """Return the reference id in *raw* (``${secret:<ref>}``), or ``None``."""
    match = _REF_RE.match(raw)
    return match.group(1) if match else None


def ref_for_path(path: str) -> str:
    """Derive a stable ref id from a dotted path (same path -> same ref)."""
    return re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_")


def env_key_for_ref(ref: str) -> str:
    """Env var name :meth:`Secret.resolve` checks first for *ref*."""
    return "HORUS_SECRET_" + re.sub(r"[^A-Za-z0-9]+", "_", ref).upper()


def _resolve(ref: str) -> str:
    """Resolve *ref*: env var first, then ``$HORUS_SECRETS_FILE``."""
    env_key = env_key_for_ref(ref)
    if env_key in os.environ:
        return os.environ[env_key]
    secrets_file = os.environ.get("HORUS_SECRETS_FILE")
    if secrets_file:
        data = yaml.safe_load(Path(secrets_file).read_text(encoding="utf-8"))
        if isinstance(data, dict) and ref in data:
            return str(data[ref])
    raise SecretResolutionError(
        f"No value for secret reference {ref!r}: set {env_key} or add it "
        f"to the file named by $HORUS_SECRETS_FILE."
    )


def _serialize(value: Any, info: core_schema.SerializationInfo) -> Any:
    """JSON: a reference as itself, a literal masked. Python: unchanged."""
    if info.mode != "json":
        return value
    if isinstance(value, Secret) and value.ref is not None:
        return value.get_secret_value()
    return str(value)  # SecretStr's mask ("" for an empty value)


_SERIALIZATION = core_schema.plain_serializer_function_ser_schema(
    _serialize, info_arg=True, when_used="always"
)


class Secret(SecretStr):
    """
    A model field marker for a credential.

    Inherits ``SecretStr``'s masked ``repr``/``str``/JSON dump, and pydantic's
    ``format: "password"`` / ``writeOnly`` in the generated JSON schema. A
    value validated from ``${secret:<ref>}`` is a *reference* (``.ref`` set,
    ``.resolve()`` looks it up); any other value is a literal (``.ref is
    None``, ``.resolve()`` returns it as-is).
    """

    # get_secret_value() is left unoverridden: pydantic's own JSON masking
    # calls it internally to build "**********", and that must not raise
    # just because this process can't resolve a reference yet. Only
    # resolve() -- called when a plugin actually needs the value -- can fail.

    __pydantic_serializer__ = SchemaSerializer(
        core_schema.any_schema(serialization=_SERIALIZATION)
    )

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[Any], handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """
        SecretStr's schema with :func:`_serialize` in place of its mask.

        A reference is not sensitive, and masking it is what turned run
        snapshots into "**********" passwords: dump it as itself.
        """
        schema = super().__get_pydantic_core_schema__(source, handler)
        for branch in ("lax_schema", "strict_schema"):
            schema[branch]["serialization"] = _SERIALIZATION
        return schema

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
            value = self.get_secret_value()
        else:
            value = _resolve(self._ref)
        if value == SECRET_MASK:
            # A masked dump copied back in as if it were the value; using it
            # would just fail later as a confusing "permission denied".
            raise SecretResolutionError(
                "Secret holds the masked placeholder "
                f"{SECRET_MASK!r}, not a real value: point the field at a "
                "stored credential instead."
            )
        return value


def iter_secret_fields(
    obj: Any, _prefix: str = ""
) -> Iterator[tuple[str, Secret]]:
    """
    Walk a model (or the lists/dicts it's built from) and yield
    ``(dotted_path, secret)`` for every ``Secret`` field reachable from it.

    Dotted paths use ``.`` even for list indices (``tasks.1.target.password``)
    so ``iter_secret_refs`` can match the same path on the dumped dict.
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


def iter_secret_refs(
    data: Any, _prefix: str = ""
) -> Iterator[tuple[str, str]]:
    """
    Walk a *dumped* workflow dict (no model classes, e.g. tc-os's opaque
    ``workflow_data``) and yield ``(dotted_path, ref)`` for every
    ``${secret:<ref>}`` string found. The dict-side twin of
    :func:`iter_secret_fields`, for a caller with no plugin models to walk.
    """
    if isinstance(data, str):
        ref = secret_ref(data)
        if ref is not None and _prefix:
            yield _prefix, ref
        return
    if isinstance(data, list):
        for i, item in enumerate(data):
            yield from iter_secret_refs(item, f"{_prefix}.{i}")
        return
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{_prefix}.{key}" if _prefix else key
            yield from iter_secret_refs(value, path)
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
    Return a deep copy of *data* with each secret field replaced by its
    ``${secret:<ref>}`` reference -- never the value, not even masked. A
    literal gets a ref derived from its path; an existing reference keeps its
    own ref, so redacting an already-redacted document is a no-op.
    """
    out = copy.deepcopy(data)
    for path, secret in secret_fields:
        ref = secret.ref or ref_for_path(path)
        _set_path(out, path.split("."), f"${{secret:{ref}}}")
    return out
