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
Promote a workflow's implicit root inputs into declared root artifacts.

A task input that no edge feeds is a *root input*: a file the author supplies
rather than one the run produces. The runtime is happy to leave that implicit
-- it just reads the path -- but nothing downstream can then tell an input
apart from an incidental path. A UI importing the workflow has no list of
"files the user provides", so it renders no input nodes.

This module makes the implicit explicit: each such input gains an entry in the
top-level ``artifacts:`` list and an edge wiring it to the task that consumes
it (the ``artifact-<rootId>`` convention, see
:meth:`horus_runtime.core.workflow.base.BaseWorkflow._assert_edge_source_resolves`).
The task's own ``inputs:`` are left alone, so ``${input_id}`` substitutions in
commands keep resolving.

The rewrite is applied to the YAML *text*, inserting two blocks, rather than
by re-dumping the model. Re-dumping would resolve every relative path to an
absolute one (see :meth:`BaseWorkflow.to_yaml`), which makes the workflow
unportable -- exactly what packaging then refuses to travel -- and would drop
the comments and the executor anchor that these files are written around.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from horus_runtime.core.workflow.base import BaseWorkflow


def _yaml_scalar(value: str) -> str:
    """Render *value* as a single-line YAML scalar, quoting when needed."""
    # safe_dump appends an explicit "...\n" document-end marker for a bare
    # top-level scalar; take just the first line, which is the scalar itself.
    return yaml.safe_dump(value, default_flow_style=True).splitlines()[0]


class SanitizeError(Exception):
    """A workflow could not be sanitized."""


@dataclass(frozen=True)
class RootInput:
    """An unwired task input, and the root artifact it would be promoted to."""

    root_id: str
    """Id for the new root artifact."""

    kind: str
    """Artifact kind, copied from the task input."""

    path: Path
    """Workflow-relative path, exactly as declared."""

    consumers: tuple[tuple[str, str], ...]
    """``(task_id, input_id)`` pairs to wire, one edge each."""

    name: str = ""
    """Display name, copied from the task input when the author set one."""

    description: str = ""
    """Description, copied from the task input when the author set one."""


@dataclass(frozen=True)
class MissingEdge:
    """A task input whose path another task produces, but no edge feeds."""

    task_id: str
    input_id: str
    path: Path
    producer: str


def _root_id(path: Path, input_id: str, task_id: str, taken: set[str]) -> str:
    """
    Pick a template-safe, unique id for the root artifact backing *path*.

    Prefers the consumer's own input id, which is what the author already
    reads in the command template. Falls back to qualifying it with the task,
    then to a numeric suffix, so two unrelated inputs that happen to share a
    name stay distinguishable.
    """
    for candidate in (input_id, f"{task_id}_{input_id}"):
        if candidate not in taken:
            return candidate
    stem = re.sub(r"[^a-z0-9]+", "_", str(path).lower()).strip("_")
    candidate = stem or input_id
    suffix = 2
    while candidate in taken:
        candidate = f"{stem}_{suffix}"
        suffix += 1
    return candidate


def find_root_inputs(
    workflow: BaseWorkflow,
) -> tuple[list[RootInput], list[MissingEdge]]:
    """
    Return ``(root_inputs, missing_edges)`` for *workflow*.

    A task input qualifies as a root input when no edge targets it and its
    declared path is relative. An input that no edge feeds but whose path
    *is* produced by some task is not a root input at all -- it is a missing
    edge, reported separately because only the author can decide whether the
    dependency or the path is wrong.

    Absolute paths are skipped: they name a location on this machine, cannot
    travel, and are the author's responsibility (the same rule
    :func:`horus_runtime.packaging.collect_bundle_paths` applies).
    """
    wired = {
        (edge.target, edge.target_input)
        for edge in workflow.edges
        if edge.target_input is not None
    }
    # Reuses the runtime's own produced-vs-external rule rather than
    # restating it here, so sanitizing can never drift from packaging.
    produced = workflow._produced_declared_paths()  # noqa: SLF001
    producer_of = {
        artifact.declared_path: task.id
        for task in workflow.tasks
        for artifact in task.outputs
        if artifact.declared_path is not None
    }

    taken = {artifact.id for artifact in workflow.artifacts}
    by_path: dict[Path, RootInput] = {}
    missing: list[MissingEdge] = []

    for task in workflow.tasks:
        for artifact in task.inputs:
            declared = artifact.declared_path
            if (task.id, artifact.id) in wired:
                continue
            if declared is None or declared.is_absolute():
                continue
            if declared in produced:
                missing.append(
                    MissingEdge(
                        task_id=task.id,
                        input_id=artifact.id,
                        path=declared,
                        producer=producer_of[declared],
                    )
                )
                continue
            # One file consumed by several tasks travels as one root
            # artifact with one edge per consumer.
            # name defaults to the artifact's own id (see
            # BaseArtifact.default_name), so only an id-differing name
            # reflects something the author actually wrote; a bare echo
            # carries no information the root_id doesn't already.
            is_echo = artifact.name == artifact.id
            authored_name = "" if is_echo else artifact.name
            existing = by_path.get(declared)
            if existing is not None:
                by_path[declared] = RootInput(
                    root_id=existing.root_id,
                    kind=existing.kind,
                    path=existing.path,
                    consumers=(*existing.consumers, (task.id, artifact.id)),
                    name=existing.name or authored_name,
                    description=existing.description or artifact.description,
                )
                continue
            root_id = _root_id(declared, artifact.id, task.id, taken)
            taken.add(root_id)
            by_path[declared] = RootInput(
                root_id=root_id,
                kind=artifact.kind,
                path=declared,
                consumers=((task.id, artifact.id),),
                name=authored_name,
                description=artifact.description,
            )

    return sorted(by_path.values(), key=lambda r: r.path), missing


def _render(root_inputs: list[RootInput]) -> tuple[list[str], list[str]]:
    """Render the ``artifacts:`` entries and ``edges:`` entries to insert."""
    artifacts: list[str] = []
    edges: list[str] = []
    for root in root_inputs:
        artifacts += [f"  - id: {root.root_id}"]
        if root.name:
            artifacts.append(f"    name: {_yaml_scalar(root.name)}")
        if root.description:
            artifacts.append(
                f"    description: {_yaml_scalar(root.description)}"
            )
        artifacts += [
            f"    kind: {root.kind}",
            f"    path: {root.path.as_posix()}",
            "",
        ]
        for task_id, input_id in root.consumers:
            edges += [
                f"  - source: artifact-{root.root_id}",
                f"    source_output: {root.root_id}",
                f"    target: {task_id}",
                f"    target_input: {input_id}",
                "",
            ]
    return artifacts, edges


def _top_level_keys(lines: list[str]) -> dict[str, int]:
    """Map each top-level mapping key to its line index."""
    keys: dict[str, int] = {}
    for i, line in enumerate(lines):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
        if match and match.group(1) not in keys:
            keys[match.group(1)] = i
    return keys


def _block_end(lines: list[str], start: int, keys: dict[str, int]) -> int:
    """Line index just past the block whose key is at *start*."""
    later = [i for i in keys.values() if i > start]
    end = min(later) if later else len(lines)
    # Don't swallow the blank line separating this block from the next key.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def apply_promotions(text: str, root_inputs: list[RootInput]) -> str:
    """
    Return *text* with the promoted artifacts and edges inserted.

    Purely additive: existing lines are never rewritten, so comments, quoting
    and YAML anchors survive untouched.
    """
    if not root_inputs:
        return text
    lines = text.splitlines()
    artifact_lines, edge_lines = _render(root_inputs)
    keys = _top_level_keys(lines)

    if "edges" not in keys:
        raise SanitizeError(
            "Workflow has no top-level 'edges:' block to extend."
        )

    # Insert the later block first so the earlier block's index stays valid.
    edges_at = _block_end(lines, keys["edges"], keys)
    lines[edges_at:edges_at] = ["", *edge_lines] if edge_lines else []

    if "artifacts" in keys:
        at = _block_end(lines, keys["artifacts"], keys)
        lines[at:at] = artifact_lines
    else:
        # Root inputs read best above the tasks that consume them; fall back
        # to the edges block for a workflow with no tasks key of its own.
        anchor = keys.get("tasks", keys["edges"])
        lines[anchor:anchor] = ["artifacts:", *artifact_lines]

    return "\n".join(lines) + "\n"


def sanitize_workflow(
    workflow_yaml: Path,
    output: Path | None = None,
    accept: "set[str] | None" = None,
) -> tuple[Path, list[RootInput], list[MissingEdge]]:
    """
    Write a copy of *workflow_yaml* with its root inputs declared.

    Returns ``(written, promoted, missing_edges)``. *accept*, when given,
    limits promotion to those root artifact ids. Nothing is written when no
    root input is promoted, and *written* is then the input path unchanged.

    The result is re-loaded before returning, so a rewrite that would not
    parse or validate fails here rather than at run time.
    """
    workflow_yaml = workflow_yaml.resolve()
    workflow = BaseWorkflow.from_yaml(workflow_yaml)
    root_inputs, missing = find_root_inputs(workflow)
    if accept is not None:
        root_inputs = [r for r in root_inputs if r.root_id in accept]
    if not root_inputs:
        return workflow_yaml, [], missing

    text = workflow_yaml.read_text(encoding="utf-8")
    output = (
        output
        or workflow_yaml.with_name(
            f"{workflow_yaml.stem}.sanitized{workflow_yaml.suffix}"
        )
    ).resolve()
    output.write_text(apply_promotions(text, root_inputs), encoding="utf-8")

    try:
        BaseWorkflow.from_yaml(output)
    except Exception as exc:
        raise SanitizeError(
            f"Sanitized workflow does not validate: {exc}"
        ) from exc

    return output, root_inputs, missing
