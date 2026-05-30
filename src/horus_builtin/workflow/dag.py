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
DAG utilities for Horus built-in workflows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from horus_runtime.core.workflow.exceptions import (
    ArtifactIdsAreNotUniqueError,
    WorkflowError,
)
from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class CyclicDependencyError(WorkflowError):
    """
    Raised when a cycle is detected in the task dependency graph.
    """

    pass


class UnknownTaskError(KeyError, WorkflowError):
    """
    Raised when a task is referenced that does not exist in the DAG.
    """

    pass


def build_artifact_producers(tasks: list[BaseTask]) -> dict[str, str]:
    """
    Returns a map of artifact_id -> task_id for every output artifact
    declared across all tasks. Raises if two tasks declare the same
    output artifact id (that would be a malformed DAG).
    """
    producers: dict[str, str] = {}
    for task in tasks:
        for artifact in task.outputs:
            if artifact.id in producers:
                raise ArtifactIdsAreNotUniqueError(artifact.id)
            producers[artifact.id] = task.id
    return producers


def build_dependencies(
    tasks: list[BaseTask],
    producers: dict[str, str],
) -> dict[str, set[str]]:
    """
    Returns a map of task_id -> set of task_ids that must complete before it.
    Input artifacts with no producer are root inputs (files, user uploads...).
    """
    deps: dict[str, set[str]] = {task.id: set() for task in tasks}
    for task in tasks:
        for artifact in task.inputs:
            upstream = producers.get(artifact.id)
            if upstream is not None:
                deps[task.id].add(upstream)
    return deps


def ancestors(
    task_id: str,
    dependencies: dict[str, set[str]],
) -> set[str]:
    """
    Returns the set of all task_ids that must run to satisfy task_id,
    inclusive of task_id itself. Walks upstream via DFS (depth-first search).
    """
    visited: set[str] = set()
    stack = [task_id]
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        stack.extend(dependencies.get(node, set()))
    return visited


def descendants(
    task_id: str,
    dependencies: dict[str, set[str]],
) -> set[str]:
    """
    Returns the set of all task_ids that depend (directly or transitively)
    on task_id, inclusive of task_id itself. Walks downstream via DFS.
    """
    # Invert the dependency map: task_id -> set of tasks that depend on it.
    dependents: dict[str, set[str]] = {t: set() for t in dependencies}
    for t, deps in dependencies.items():
        for dep in deps:
            dependents.setdefault(dep, set()).add(t)

    visited: set[str] = set()
    stack = [task_id]
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        stack.extend(dependents.get(node, set()))
    return visited


def topological_sort(
    task_ids: set[str],
    dependencies: dict[str, set[str]],
) -> list[str]:
    """
    Kahn's algorithm on the subgraph defined by task_ids.
    Raises CyclicDependencyError if a cycle is detected.
    """
    sub_deps: dict[str, set[str]] = {
        t: dependencies[t] & task_ids for t in task_ids
    }
    in_degree: dict[str, int] = {t: len(sub_deps[t]) for t in task_ids}

    dependents: dict[str, set[str]] = {t: set() for t in task_ids}
    for t, deps in sub_deps.items():
        for dep in deps:
            dependents[dep].add(t)

    # Sorted for deterministic output when multiple tasks are unblocked.
    queue: list[str] = sorted(t for t in task_ids if in_degree[t] == 0)
    result: list[str] = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for dependent in sorted(dependents[node]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(result) != len(task_ids):
        raise CyclicDependencyError(
            _("Cycle detected among tasks: " + str(task_ids - set(result)))
        )

    return result


def execution_plan(
    tasks: list[BaseTask],
    trigger_id: str | None = None,
) -> list[str]:
    """
    Returns an ordered list of task_ids to execute.

    If trigger_id is None, the full DAG is executed in topological order.
    If trigger_id is given, the scope is the trigger task plus its ancestors
    (upstream tasks needed to provide its inputs) and its descendants
    (downstream tasks that consume its outputs). Unrelated branches are
    excluded entirely. Ancestors whose outputs already exist are skipped at
    run time by ``BaseTask.run`` via ``is_complete``.

    Raises UnknownTaskError if trigger_id is not in tasks.
    """
    task_ids = {task.id for task in tasks}
    if trigger_id is not None and trigger_id not in task_ids:
        raise UnknownTaskError(f"Trigger task '{trigger_id}' not found.")

    producers = build_artifact_producers(tasks)
    deps = build_dependencies(tasks, producers)
    if trigger_id is None:
        scope = task_ids
    else:
        scope = ancestors(trigger_id, deps) | descendants(trigger_id, deps)
    return topological_sort(scope, deps)
