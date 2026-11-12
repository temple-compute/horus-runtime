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
Launcher shipped to the target by
:class:`~horus_builtin.executor.python_fn_external.
ExternalPythonFunctionExecutor`.

It is copied to the task working directory and run there by the target's
interpreter, so it is deliberately a *script*: it must import nothing from
horus-runtime at module level (the payload may pull horus classes in while
unpickling, but the launcher itself must start on any interpreter that has
cloudpickle).

Protocol, both halves of which are hard-coded here and in the executor:

- ``argv[1]``: cloudpickled ``(func, kwargs)`` payload to read.
- ``argv[2]``: where to write the JSON outcome.
- outcome: ``{"ok": bool, "value": ..., "traceback": str | None}``. Artifacts
  in ``value`` are encoded as ``{"__horus_artifact__": <model dump>}``.

The outcome is plain JSON, never a pickle: the orchestrator reads it back from
the target and must not run code while decoding it.

The outcome file, not the exit code, is what the executor believes: an exit
code can only say *that* something failed, and "subprocess exited 1" is a
useless thing to hand a scientist whose function raised on line 40.
"""

import asyncio
import inspect
import json
import sys
import traceback
from collections.abc import Awaitable
from typing import Any

import cloudpickle

ARTIFACT_KEY = "__horus_artifact__"


def _encode(value: Any) -> Any:
    """Tag artifacts so the executor can rebuild them; the rest stays as is."""
    base = sys.modules.get("horus_runtime.core.artifact.base")
    if base is not None and isinstance(value, base.BaseArtifact):
        return {ARTIFACT_KEY: value.model_dump(mode="json")}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


async def _resolve(awaitable: Awaitable[Any]) -> Any:
    """Await *awaitable*, the entry point ``asyncio.run`` needs."""
    return await awaitable


def _write_outcome(result_path: str, outcome: dict[str, Any]) -> None:
    """
    Write *outcome* to *result_path* as JSON, failing the call with a clear
    message when the return value is not JSON-serialisable.
    """
    try:
        data = json.dumps({**outcome, "value": _encode(outcome["value"])})
    except Exception:
        data = json.dumps(
            {
                "ok": False,
                "value": None,
                "traceback": (
                    "Could not send the result back to the orchestrator: "
                    "the return value is not JSON-serialisable. Return an "
                    "artifact, a list of artifacts, None, or plain JSON "
                    "data.\n"
                    f"{traceback.format_exc()}"
                ),
            }
        )
    with open(result_path, "w", encoding="utf-8") as fh:
        fh.write(data)


def main(payload_path: str, result_path: str) -> int:
    """
    Call the pickled function and report the outcome. Returns an exit code.
    """
    with open(payload_path, "rb") as fh:
        func, kwargs = cloudpickle.load(fh)

    outcome: dict[str, Any]
    try:
        value = func(**kwargs)
        # The in-process executor awaits coroutine functions, so an async task
        # body must keep working here.
        if inspect.isawaitable(value):
            value = asyncio.run(_resolve(value))
        outcome = {"ok": True, "value": value, "traceback": None}
    except Exception:
        outcome = {
            "ok": False,
            "value": None,
            "traceback": traceback.format_exc(),
        }

    _write_outcome(result_path, outcome)
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
