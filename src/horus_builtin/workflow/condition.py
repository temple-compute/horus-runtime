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
Evaluation of edge conditions, and the liveness rule built on top of them.

A conditional workflow is a DAG in which some edges are gated. The rule that
decides what runs:

    A task is **live** iff it has no incoming task-edges, or at least one
    incoming task-edge is live. An edge is live iff its source task is live
    and its condition (if any) evaluates true.

This is an OR-join (BPMN inclusive merge), and it is chosen because it makes
the two shapes users actually draw both behave:

``A -> (B | C) -> D`` (diamond)
    Taking ``B`` leaves ``C`` dead, but ``D`` still has a live incoming edge
    from ``B``, so the join runs. An "all parents must be live" rule would
    strand every join behind a branch.

``C -> E`` (chain below a dead branch)
    ``E`` has no condition of its own, but its only incoming edge comes from a
    dead task, so ``E`` is dead too. Deactivation propagates without any
    explicit marking.

Liveness depends only on the tasks, the edges, and the condition values, so the
canvas can compute exactly the same result client-side and grey out the paths
that will not be taken.

Known limitation, deliberately not solved here: a task that genuinely needs
*all* of its parents will still run when only some are live, and will fail on
the missing input. If that shape becomes common, the fix is a per-task
``join_policy`` rather than a change to this rule.
"""

import json
from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING, Any

from horus_runtime.core.workflow.condition import (
    EdgeCondition,
    PythonCondition,
)
from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.workflow.base import BaseWorkflow
    from horus_runtime.core.workflow.edge import WorkflowEdge


class ConditionEvaluationError(WorkflowError):
    """
    Raised when a condition cannot be evaluated at all.

    Deliberately fatal rather than defaulting to "not live": a predicate that
    cannot be read is a broken workflow, and silently pruning the branch would
    hide it behind a run that looks successful.
    """

    pass


def _walk(document: Any, key: str | None) -> Any:
    """
    Follow a dotted path into a decoded JSON document.

    Returns ``None`` for a path that does not resolve, so ``exists`` can tell
    absent from false without raising.
    """
    if key is None:
        return document
    current = document
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "exists": lambda actual, _expected: actual is not None,
    "truthy": lambda actual, _expected: bool(actual),
    "eq": lambda actual, expected: bool(actual == expected),
    "ne": lambda actual, expected: bool(actual != expected),
    "in": lambda actual, expected: actual in expected,
    "not_in": lambda actual, expected: actual not in expected,
    "lt": lambda actual, expected: bool(actual < expected),
    "le": lambda actual, expected: bool(actual <= expected),
    "gt": lambda actual, expected: bool(actual > expected),
    "ge": lambda actual, expected: bool(actual >= expected),
}
"""
The operator table. A dict rather than a match statement so that the set of
operators has exactly one definition, which ``ConditionOp`` mirrors.
"""


def _apply(op: str, actual: Any, expected: Any) -> bool:
    """
    Apply one comparison from the closed operator set.
    """
    try:
        compare = _OPS[op]
    except KeyError as exc:
        raise ConditionEvaluationError(
            _("Unknown condition operator '%(op)s'.") % {"op": op}
        ) from exc

    try:
        return compare(actual, expected)
    except TypeError as exc:
        # Ordering an int against a string, or testing membership in a
        # non-collection: a workflow bug, not an evaluation that is merely
        # false.
        raise ConditionEvaluationError(
            _("Cannot compare %(actual)r and %(expected)r with '%(op)s'.")
            % {"actual": actual, "expected": expected, "op": op}
        ) from exc


async def _read_source_document(
    wf: "BaseWorkflow",
    task_id: str,
    output_id: str,
) -> Any:
    """
    Read and decode the JSON sentinel an upstream task wrote.

    Mirrors ``LoopController._read_signal``: resolve the artifact on the
    producing task's own target, confirm it was written, then decode it.
    """
    task = next((t for t in wf.tasks if t.id == task_id), None)
    if task is None:
        raise ConditionEvaluationError(
            _("Condition references unknown task '%(task)s'.")
            % {"task": task_id}
        )

    artifact = next(
        (a for a in task.outputs if a.id == output_id),
        None,
    )
    if artifact is None:
        raise ConditionEvaluationError(
            _(
                "Condition references output '%(output)s', which task "
                "'%(task)s' does not declare."
            )
            % {"output": output_id, "task": task_id}
        )

    path = task.target.path_on_target(artifact)
    if not await task.target.path_exists(path):
        raise ConditionEvaluationError(
            _(
                "Condition reads output '%(output)s' of task '%(task)s', "
                "which was never written."
            )
            % {"output": output_id, "task": task_id}
        )

    raw = await task.target.get_file(path)
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConditionEvaluationError(
            _(
                "Condition reads output '%(output)s' of task '%(task)s', "
                "which is not valid JSON."
            )
            % {"output": output_id, "task": task_id}
        ) from exc


def _resolve_ref(ref: str) -> Any:
    """
    Import a ``module:qualname`` reference.

    Used when a Python condition crossed a process boundary (the canvas, the
    orchestrator) and lost its in-memory callable.
    """
    module_name, _sep, qualname = ref.partition(":")
    if not module_name or not qualname:
        raise ConditionEvaluationError(
            _(
                "Python condition reference '%(ref)s' is not of the form "
                "'module:qualname'."
            )
            % {"ref": ref}
        )
    try:
        obj: Any = import_module(module_name)
    except ImportError as exc:
        raise ConditionEvaluationError(
            _(
                "Python condition '%(ref)s' cannot be imported. The module "
                "must be installed in this run's environment."
            )
            % {"ref": ref}
        ) from exc

    for part in qualname.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise ConditionEvaluationError(
                _("Python condition '%(ref)s' does not exist.") % {"ref": ref}
            ) from exc
    return obj


async def evaluate_condition(
    wf: "BaseWorkflow",
    edge: "WorkflowEdge",
) -> bool:
    """
    Evaluate one edge's condition. An edge with no condition is always taken.
    """
    condition = edge.condition
    if condition is None:
        return True

    # Both forms resolve their sentinel the same way: the condition's own
    # source if it names one, else the edge's endpoints.
    task_id = condition.source_task or edge.source
    output_id = condition.source_output or edge.source_output

    if isinstance(condition, EdgeCondition):
        if output_id is None:
            raise ConditionEvaluationError(
                _(
                    "Condition on edge '%(source)s' -> '%(target)s' names no "
                    "output to read."
                )
                % {"source": edge.source, "target": edge.target}
            )
        document = await _read_source_document(wf, task_id, output_id)
        return _apply(
            condition.op,
            _walk(document, condition.key),
            condition.value,
        )

    if isinstance(condition, PythonCondition):
        func = condition.func
        if func is None:
            if condition.ref is None:
                raise ConditionEvaluationError(
                    _(
                        "Python condition on edge '%(source)s' -> "
                        "'%(target)s' has neither a callable nor a reference."
                    )
                    % {"source": edge.source, "target": edge.target}
                )
            func = _resolve_ref(condition.ref)

        # The callable sees the same document a declarative condition would,
        # so the two forms are interchangeable from the workflow's side. A
        # source that wrote nothing yields None rather than raising: unlike the
        # declarative form, a Python predicate may legitimately want to decide
        # on absence.
        payload: Any = None
        if output_id is not None:
            try:
                payload = await _read_source_document(wf, task_id, output_id)
            except ConditionEvaluationError:
                payload = None

        result = func(payload)
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)

    raise ConditionEvaluationError(
        _("Unsupported condition type %(type)s.")
        % {"type": type(condition).__name__}
    )


async def compute_liveness(
    wf: "BaseWorkflow",
    task_id: str,
    cache: dict[str, bool],
) -> bool:
    """
    Whether ``task_id`` should run, per the OR-join rule described above.

    ``cache`` is the scheduler's per-run memo, and doubles as the record of
    what has already been decided. By the time the scheduler asks about a task,
    every parent has passed through this gate, so a cache miss means the parent
    is outside the run's scope (or is a root) and is treated as live.

    Conditions on edges whose source is already dead are **not** evaluated: the
    dead task never wrote the sentinel those conditions would read, so
    evaluating them would raise instead of simply pruning the branch.
    """
    if task_id in cache:
        return cache[task_id]

    task_ids = {t.id for t in wf.tasks}
    incoming = [
        edge
        for edge in wf.edges
        if edge.target == task_id and edge.source in task_ids
    ]

    # No task feeds this one: it is a root (or fed only by root artifacts), so
    # there is nothing that could have deactivated it.
    if not incoming:
        cache[task_id] = True
        return True

    live = False
    for edge in incoming:
        if not cache.get(edge.source, True):
            continue
        if await evaluate_condition(wf, edge):
            live = True
            break

    cache[task_id] = live
    return live
