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
Run a ``python_function`` runtime in a subprocess **on the task's target**
instead of inside the orchestrator.

Why this exists, given ``python_fn`` already runs functions:

- ``PythonFunctionExecutor`` ignores ``task.target``. A function task with an
  SSH target runs on the orchestrator, which is a correctness bug and not
  merely a metrics gap. This executor honours the target like every other
  executor does, by going through ``target.run_command``.
- Because the work is a real process, it inherits the default
  :meth:`BaseExecutor.resource_scope` (a ``ProcessTreeScope`` with a live
  pid) and becomes measurable with no extra code here.

**Limitation: live DAG mutation does not work here.** A task body may call
``workflow.add_task()`` / ``add_edge()`` on the running workflow (see
``core/workflow/base.py``, "Safe to call from inside a running task"). That
depends on sharing memory with the orchestrator and cannot cross a process
boundary, so tasks that mutate the DAG must keep using the in-process
``python_function`` executor. The same reasoning applies to any function that
takes the live ``task``; rather than hand it a lookalike copy whose mutations
would silently go nowhere, this executor refuses such a function up front.
"""

import asyncio
import json
import shlex
import sys
import tempfile
from contextlib import aclosing
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar, TypeGuard

import cloudpickle

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor._remote_function_call import ARTIFACT_KEY
from horus_builtin.executor.python_fn import parse_result_artifacts
from horus_builtin.runtime.python import PythonFunctionRuntime
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.executor.base import (
    BaseExecutor,
    RuntimeFilterType,
    _is_safe_component,
)
from horus_runtime.core.task.exceptions import (
    TargetLostError,
    TaskExecutionError,
)
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.settings import runtime_settings

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask

_LAUNCHER_SOURCE = Path(__file__).with_name("_remote_function_call.py")
_LAUNCHER_NAME = ".horus_function_call.py"
_PAYLOAD_NAME = ".horus_function_payload.pkl"
_RESULT_NAME = ".horus_function_result.json"
_RETURNABLE: dict[str, type[FileArtifact] | type[FolderArtifact]] = {
    "file": FileArtifact,
    "folder": FolderArtifact,
}


def _is_tagged(value: Any) -> TypeGuard[dict[str, Any]]:
    """True for an artifact descriptor the launcher tagged."""
    return isinstance(value, dict) and value.keys() == {ARTIFACT_KEY}


class ExternalPythonFunctionExecutor(BaseExecutor):
    """
    Execute a Python function in a subprocess on the task's target.
    """

    kind: str = "python_function_external"
    kind_name: ClassVar[str] = "External Python Function Executor"
    kind_description: ClassVar[str] = _(
        "Executes a Python function in a subprocess on the task's target."
    )

    runtimes: ClassVar[RuntimeFilterType] = (PythonFunctionRuntime,)

    python: str | None = None
    """
    Interpreter used on the target, a single executable path or name (it is
    shell-quoted, not split). It needs ``cloudpickle`` importable, plus
    whatever the function itself imports, including ``horus_runtime`` if the
    function takes or returns artifacts.

    ``None`` (the default) means: the orchestrator's own interpreter when the
    target is co-located with it, otherwise plain ``python``. The first is the
    one interpreter that provably satisfies those requirements, since it is
    running this code; the second is the only thing that can be assumed about
    a machine we have never seen.

    Environment management is deliberately *not* reimplemented here. The
    ``horus-environments`` executors already build conda/uv/virtualenv
    environments on the target and put the interpreter at a known path, so
    composing with them is a matter of pointing this field at one, e.g.
    ``python: .horus_python_environment/bin/python``. Relative paths resolve
    against the task working directory, which is the command's cwd.
    """

    def _interpreter(self, task: "BaseTask") -> str:
        """Return the interpreter command to invoke on the target."""
        if self.python is not None:
            return self.python
        if task.target.is_orchestrator_local:
            return sys.executable
        return "python"

    async def _execute(self, task: "BaseTask") -> None:
        """
        Ship the function to the target, run it there, bring the result back.
        """
        assert isinstance(task.runtime, PythonFunctionRuntime)
        func, kwargs = await task.runtime.setup_runtime(task)

        if "task" in kwargs:
            # PythonFunctionRuntime injects the live task whenever the
            # signature asks for it. A copy of it on the far side of a pipe
            # would look right and do nothing (mutations, DAG edits and
            # interactions all die with the subprocess), so fail loudly here
            # instead of subtly there.
            raise TaskExecutionError(
                _(
                    "Task %(task_id)s uses the '%(kind)s' executor but its "
                    "function accepts a 'task' argument. The live task object "
                    "cannot cross a process boundary; use the in-process "
                    "'python_function' executor, or drop the 'task' parameter "
                    "(and any **kwargs that would receive it)."
                )
                % {"task_id": task.id, "kind": self.kind}
            )

        await task.target.mkdir(task.working_dir)

        # cloudpickle rather than pickle: closing over local state is a
        # supported way to build a workflow programmatically, and plain pickle
        # cannot represent a closure. It also already prefers a by-reference
        # pickle for anything importable, so the cheap payload is the default
        # and the expensive one only appears when it is the only option.
        payload = f"{task.working_dir}/{_PAYLOAD_NAME}"
        result_path = f"{task.working_dir}/{_RESULT_NAME}"
        launcher = f"{task.working_dir}/{_LAUNCHER_NAME}"
        await task.target.put_file(cloudpickle.dumps((func, kwargs)), payload)
        await task.target.put_file(_LAUNCHER_SOURCE, launcher)

        command = (
            f"{shlex.quote(self._interpreter(task))} {shlex.quote(launcher)} "
            f"{shlex.quote(payload)} {shlex.quote(result_path)}"
        )
        horus_logger.log.debug(
            _("Executing function for task %(task_id)s: %(command)s")
            % {"task_id": task.id, "command": command}
        )

        proc = await task.target.run_command(
            command,
            cwd=task.working_dir,
            env={
                runtime_settings.SIDE_ARTIFACTS_DIR_ENV: str(
                    task.side_artifacts_dir
                ),
            },
        )
        try:
            async with aclosing(proc.stream()) as stream:
                async for stream_name, line in stream:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if stream_name == "stdout":
                        horus_logger.log.info(text)
                    else:
                        horus_logger.log.warning(text)
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        await proc.wait()

        outcome = await self._read_outcome(task, result_path, proc.returncode)
        value = outcome.get("value")
        if _is_tagged(value):
            value = await self._fetch_returned(task, value[ARTIFACT_KEY])
        elif isinstance(value, list) and value and all(map(_is_tagged, value)):
            value = [
                await self._fetch_returned(task, item[ARTIFACT_KEY])
                for item in value
            ]
        await parse_result_artifacts(task, value)

    async def _fetch_returned(
        self, task: "BaseTask", descriptor: Any
    ) -> BaseArtifact:
        """
        Bring back one artifact the function returned, trusting nothing in it
        but a relative path and an id.

        The descriptor was written on the target. Only ``file`` and ``folder``
        kinds the task declares as outputs are accepted, the path must be
        relative to the task working directory with no ``..``, and every
        component is looked up with ``list_dir`` (which skips symlinks). The
        data is then pulled over the channel into a local directory, and that
        copy is what gets registered, never the path the target sent.
        """
        declared = {artifact.kind for artifact in task.outputs}
        kind = descriptor.get("kind") if isinstance(descriptor, dict) else None
        artifact_cls = (
            _RETURNABLE.get(kind)
            if isinstance(kind, str) and kind in declared
            else None
        )
        raw_path = descriptor.get("path") if artifact_cls else None
        artifact_id = descriptor.get("id") if artifact_cls else None
        rel = PurePosixPath(raw_path) if isinstance(raw_path, str) else None
        if (
            artifact_cls is None
            or not isinstance(artifact_id, str)
            or rel is None
            or rel.is_absolute()
            or not rel.parts
            or not all(_is_safe_component(part) for part in rel.parts)
        ):
            raise TaskExecutionError(
                _(
                    "Task %(task_id)s returned an artifact that is not "
                    "accepted. A returned artifact must be a file or folder "
                    "kind the task declares as an output, with a path "
                    "relative to the task working directory and no '..'."
                )
                % {"task_id": task.id}
            )

        # shortcut: a target that swaps a checked path for a symlink between
        # list_dir and the copy wins the race; hosts that need more copy as
        # the target's own uid.
        current = str(task.working_dir)
        entry = None
        for index, part in enumerate(rel.parts):
            entries = await task.target.list_dir(current)
            entry = next((e for e in entries if e.name == part), None)
            if entry is None or (
                index < len(rel.parts) - 1 and not entry.is_dir
            ):
                entry = None
                break
            current = entry.path
        if entry is None or entry.is_dir != (artifact_cls is FolderArtifact):
            raise TaskExecutionError(
                _(
                    "Task %(task_id)s returned %(kind)s artifact '%(path)s', "
                    "which does not exist in its working directory."
                )
                % {"task_id": task.id, "kind": kind, "path": rel}
            )

        cap = runtime_settings.MAX_SIDE_ARTIFACT_BYTES
        local = Path(tempfile.mkdtemp(prefix="horus-returned-")) / rel.name
        if entry.is_dir:
            await self._pull_tree(task, entry.path, local, cap)
        elif entry.size > cap:
            raise TaskExecutionError(
                _(
                    "Task %(task_id)s returned file '%(path)s' of %(size)d "
                    "bytes, over the %(cap)d byte limit."
                )
                % {
                    "task_id": task.id,
                    "path": rel,
                    "size": entry.size,
                    "cap": cap,
                }
            )
        else:
            await task.target.get_file_to(entry.path, local)
        return artifact_cls(id=artifact_id, path=local)

    async def _read_outcome(
        self, task: "BaseTask", result_path: str, returncode: int | None
    ) -> dict[str, Any]:
        """
        Read the launcher's outcome file and re-raise a remote failure here.

        The exit code is only a fallback: what the caller needs is the
        target-side traceback of the exception the function raised. The file
        is JSON, never a pickle: it was written on the target and decoding it
        must not run code here.
        """
        try:
            raw = await task.target.get_file(result_path)
            outcome = json.loads(raw)
            if not isinstance(outcome, dict):
                raise ValueError(_("malformed outcome file"))
        except TargetLostError:
            # The machine is gone, not the outcome: keep the type so the
            # scheduler can retry.
            raise
        except Exception as exc:
            # No outcome file at all: the interpreter never got far enough to
            # write one (missing python, missing cloudpickle, killed job). The
            # streamed stderr above is the real diagnosis; point at it.
            raise TaskExecutionError(
                _(
                    "Function subprocess for task %(task_id)s produced no "
                    "result (exit code %(code)s); see the task log for the "
                    "interpreter's own output. Cause: %(err)s"
                )
                % {
                    "task_id": task.id,
                    "code": returncode,
                    "err": exc,
                }
            ) from exc

        if outcome.get("ok") is True:
            return outcome

        remote_traceback = outcome.get("traceback") or ""
        raise TaskExecutionError(
            _("Function failed on target %(target)s:\n%(traceback)s")
            % {
                "target": task.target.location_id,
                "traceback": remote_traceback,
            }
        )
