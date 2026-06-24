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

import heapq
from typing import TYPE_CHECKING

from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask
    from horus_runtime.core.workflow.edge import WorkflowEdge


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


def build_dependencies(
    tasks: list["BaseTask"],
    edges: list["WorkflowEdge"] | None = None,
) -> dict[str, set[str]]:
    """
    Returns a map of task_id -> set of task_ids that must complete before it.

    Edges are the sole source of truth: a ``source`` task must complete before
    its ``target`` task. Edges whose source is a root artifact (not a task) are
    ignored as they are root inputs. A workflow with no edges yields
    independent tasks (no dependencies).
    """
    deps: dict[str, set[str]] = {task.id: set() for task in tasks}

    # Build the dependency map from edges. `deps` keys are exactly the task
    # ids, so membership in `deps` doubles as "this endpoint is a task" (root
    # sources are not tasks and are skipped). No edges => independent tasks.
    for edge in edges or ():
        if edge.source in deps and edge.target in deps:
            deps[edge.target].add(edge.source)

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
    heap = [t for t in task_ids if in_degree[t] == 0]
    heapq.heapify(heap)  # O(V)

    result: list[str] = []
    while heap:
        node = heapq.heappop(heap)  # O(log V)
        result.append(node)
        for dependent in dependents[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                heapq.heappush(heap, dependent)  # O(log V)

    # Total complexity: O((V + E) x log V)
    # where V = len(task_ids) and E = sum of dependencies among task_ids.

    if len(result) != len(task_ids):
        remaining = sorted(task_ids - set(result))
        raise CyclicDependencyError(
            _("Cycle detected among tasks: %(tasks)s") % {"tasks": remaining}
        )

    return result


def execution_plan(
    tasks: list["BaseTask"],
    trigger_id: str,
    edges: list["WorkflowEdge"] | None = None,
) -> list[str]:
    """
    Returns an ordered list of task_ids to execute.

    The scope is the trigger task plus all downstream
    tasks that (transitively) depend on it, and any upstream dependencies
    required to execute those downstream tasks. Unrelated branches are excluded
    entirely. Tasks whose outputs already exist are skipped at run time by
    ``BaseTask.run`` via ``is_complete``.

    Dependencies come from ``edges``. Raises UnknownTaskError if trigger_id is
    not in tasks.
    """
    task_ids = {task.id for task in tasks}
    if trigger_id not in task_ids:
        raise UnknownTaskError(f"Trigger task '{trigger_id}' not found.")

    deps = build_dependencies(tasks, edges)
    scope = ancestors(trigger_id, deps) | descendants(trigger_id, deps)
    return topological_sort(scope, deps)
