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
Declarative conditional-repeat loop construct.

A ``loop`` task repeatedly injects one clone of a template "body" task,
forward-only, for as long as the previous iteration's body says to continue
(a predicate) and the hard ``max_iterations`` safety bound has not been
reached. It is expressed either as a ``loop:`` block in YAML (lowered by
:func:`lower_loop_entry`, hooked into
:class:`~horus_runtime.core.workflow.base.BaseWorkflow`'s ``model_validate``
pipeline) or via the :func:`loop_task` Python builder (``wf.loop(...)``).

Both authoring paths produce the same object: a :class:`LoopController`, a
registered ``kind="loop_controller"`` task whose repeat behaviour is entirely
declarative (a plain ``dict`` body template plus a handful of scalar fields),
so it round-trips through ``to_yaml``/``from_yaml`` like any other task —
unlike a Python-closure task, whose function cannot serialize
(``PythonFunctionRuntime.func`` is ``exclude=True``).

The bounded/counted case ("run this exactly N times") is already covered by
:mod:`horus_builtin.workflow.map` in range mode (``map: {range: N, ...}``):
it fans N clones out *concurrently* with a known count. This module instead
covers the *conditional* case — repeat while a predicate holds, discovered
only as each iteration completes — which is inherently sequential: iteration
``k + 1`` cannot be built until iteration ``k`` has run and reported whether
to continue. ``max_iterations`` remains a hard, always-enforced upper bound
on top of the predicate, so a runaway predicate cannot grow the graph
unboundedly.

Predicate convention (the "sentinel artifact")
-----------------------------------------------
Since a raw Python closure cannot serialize into YAML, the predicate must be
expressible declaratively. The convention used here: the body template
declares one output, named by ``signal_output``, that the body task itself
writes as small JSON object shaped ``{"continue": <bool>}`` — ``true`` to run
another iteration, ``false`` to stop. The controller reads this file
directly off the completed body task's target (via
``target.get_file``, mirroring how :class:`.MapExpander` reads its source
collection) once that iteration finishes, and uses it to decide whether to
inject the next one. This keeps the predicate itself pure data (no code),
so a loop round-trips through YAML exactly like any other declarative
construct.

The very first iteration always runs unconditionally (there is no prior
body to have written a signal yet) — a "run, then check" (do-while)
semantics, matching what "repeat while a predicate holds" means for the
very first pass.

Forward-only injection
------------------------
Each dispatch of a :class:`LoopController` instance injects, via
:meth:`~horus_runtime.core.workflow.base.BaseWorkflow.expand`, at most two
new nodes: the next body clone (id ``f"{loop_id}#{k}"``) and a fresh
"controller-check" :class:`LoopController` instance (id
``f"{loop_id}~{k}"``) that will run after that body clone completes and
decide whether to keep going. Every edge added is ``transfer=False``
(ordering-only, exactly like :class:`.MapExpander`'s wiring): it exists
purely so the new nodes fall inside the scheduler's trigger-reachable scope
(which only follows real task-to-task edges), never to source a generic
artifact transfer. Because injection only ever adds edges *forward* — from
an already-existing node to a brand new one — it can never close a cycle, so
:meth:`~horus_runtime.core.workflow.base.BaseWorkflow.add_edge`'s cycle
check inside ``expand()`` always passes trivially.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import Field, model_validator

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.store import ArtifactStore
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

if TYPE_CHECKING:
    from horus_runtime.core.workflow.base import BaseWorkflow

_FANOUT_SUFFIX = ".fanout"
_LOOP_IN_SUFFIX = ".loop_in"


class LoopConfigurationError(WorkflowError):
    """Raised when a ``loop:`` block or ``wf.loop(...)`` call is
    malformed, or a body task breaks the sentinel-artifact predicate
    convention.
    """


class LoopController(HorusTask):
    """
    Conditionally repeats a template "body" task, one iteration at a time,
    while a sentinel artifact says to continue. See the module docstring
    for the full wiring story and the predicate convention.
    """

    kind: str = "loop_controller"
    kind_name: ClassVar[str] = "Loop Controller"
    kind_description: ClassVar[str] = _(
        "Repeats a template body task while a sentinel artifact says to "
        "continue, up to a hard max_iterations safety bound."
    )

    runtime: BaseRuntime = Field(
        default_factory=lambda: CommandRuntime(command="true")
    )
    """
    Inert placeholder: :meth:`_run` is fully overridden and never delegates
    to ``self.executor``/``self.runtime``, so these exist only to satisfy
    ``BaseTask``'s required fields.
    """

    executor: BaseExecutor = Field(default_factory=ShellExecutor)
    target: BaseTarget = Field(default_factory=LocalTarget)

    loop_id: str
    """
    Stable id-prefix shared by every :class:`LoopController` instance
    belonging to one loop (the id of the original, user-authored controller
    task). Body clones are id'd ``f"{loop_id}#{k}"``; internally-injected
    controller-check instances are id'd ``f"{loop_id}~{k}"``.
    """

    body_template: dict[str, Any]
    """
    Declarative body of the per-iteration task: everything a task needs
    minus ``id``/``name`` (``kind``, ``inputs``, ``outputs``, ``runtime``,
    ``executor``, ``target``, ...). Stored as a plain, JSON-safe dict so it
    round-trips through YAML untouched; reconstructed into a real
    :class:`~horus_runtime.core.task.base.BaseTask` for each iteration via
    ``BaseTask.model_validate``. Must declare an output matching
    ``signal_output`` and, when relevant, an input matching ``index_input``.
    """

    signal_output: str
    """
    Output id on ``body_template`` the body task writes each iteration as a
    small JSON object ``{"continue": bool}`` — the loop's predicate. See the
    module docstring's "Predicate convention" section.
    """

    max_iterations: int = Field(gt=0)
    """
    Hard cap on the total number of body iterations. Always enforced on top
    of the predicate, so a body that never signals ``continue: false``
    still halts deterministically instead of growing the graph forever.
    """

    index_input: str | None = None
    """
    Optional input id on ``body_template`` that receives the iteration's
    0-based integer index as a small JSON file, materialized the same way
    :class:`.MapExpander` materializes a range-mode clone's index.
    """

    iteration: int = Field(default=0, ge=0)
    """
    ``0`` for the loop's original, user-authored controller: no body has run
    yet, so the first iteration always runs unconditionally (do-while
    semantics — there is nothing to check yet). ``k >= 1`` for an
    internally-injected controller-check instance dispatched right after
    body ``f"{loop_id}#{k}"`` completes; it reads that body's
    ``signal_output`` to decide whether to inject iteration ``k + 1``.
    """

    @property
    def _fanout_marker_id(self) -> str:
        """
        Id of this instance's internal wiring-only output marker, used to
        source the very first body's ordering edge when ``iteration == 0``
        (there is no prior body task to source it from yet).
        """
        return f"{self.id}{_FANOUT_SUFFIX}"

    @property
    def _loop_in_marker_id(self) -> str:
        """
        Id of this instance's internal wiring-only input marker, used as the
        ordering edge's target when this instance is a controller-check
        (fed from the body task it is downstream of).
        """
        return f"{self.id}{_LOOP_IN_SUFFIX}"

    @model_validator(mode="after")
    def _ensure_markers(self) -> Self:
        """
        Append the internal wiring-only markers if not already present
        (idempotent, so re-loading an already-dumped controller does not
        duplicate them). Both are deliberately never written to disk.

        - The fanout *output* marker (:attr:`_fanout_marker_id`) is added to
          every instance: the original controller sources its first body's
          ordering edge from it.
        - The loop-in *input* marker (:attr:`_loop_in_marker_id`) is added
          only to controller-check instances (``iteration >= 1``), which
          receive the ordering edge from the body they follow. The original
          controller (``iteration == 0``) is the run's trigger with no
          incoming edge, so giving it an input marker would wrongly make it
          a root input the scheduler tries to transfer from the
          orchestrator.
        """
        fanout_id = self._fanout_marker_id
        if not any(o.id == fanout_id for o in self.outputs):
            self.outputs.append(
                FileArtifact(id=fanout_id, path=Path(f"{fanout_id}.marker"))
            )
        if self.iteration >= 1:
            loop_in_id = self._loop_in_marker_id
            if not any(a.id == loop_in_id for a in self.inputs):
                self.inputs.append(
                    FileArtifact(
                        id=loop_in_id, path=Path(f"{loop_in_id}.marker")
                    )
                )
        return self

    async def is_complete(self) -> bool:
        """
        Always incomplete: a fresh :class:`LoopController` instance is
        created for every iteration boundary, so each one only ever needs to
        run once, and never needs to report complete to be skipped.
        """
        return False

    async def _reset(self) -> None:
        """
        Delete the (never-written) wiring markers, mirroring
        :meth:`~horus_builtin.task.horus_task.HorusTask._reset`. Injected
        body/controller-check instances are re-derived from scratch on every
        :meth:`_run` and reset independently as ordinary tasks.
        """
        store = ArtifactStore(self.target)
        for artifact in self.outputs:
            await store.delete(artifact)
        self.runs = 0

    async def _run(self) -> None:
        """
        Decide whether to continue (always, for the first/original
        controller; by reading the previous body's sentinel artifact
        otherwise), and, if so and the safety bound allows it, atomically
        inject the next body clone plus a controller-check instance
        downstream of it. See the module docstring for the full algorithm.
        """
        wf = self.workflow
        if wf is None:
            raise LoopConfigurationError(
                _("Loop task '%(id)s' must run inside a workflow.")
                % {"id": self.id}
            )
        orchestrator = wf.orchestrator_target
        if orchestrator is None:
            raise LoopConfigurationError(
                _(
                    "Loop task '%(id)s' requires workflow.orchestrator_"
                    "target to be set to materialize per-iteration inputs."
                )
                % {"id": self.id}
            )

        self.runs += 1

        should_continue = (
            True if self.iteration == 0 else await self._read_signal(wf)
        )
        next_iteration = self.iteration + 1

        if not should_continue or next_iteration > self.max_iterations:
            horus_logger.log.debug(
                _(
                    "Loop '%(id)s' stopping after %(n)d iteration(s) "
                    "(continue=%(cont)s)."
                )
                % {
                    "id": self.loop_id,
                    "n": self.iteration,
                    "cont": should_continue,
                }
            )
            return

        body, anchor_input = await self._build_body_clone(
            wf, orchestrator, next_iteration
        )
        checker = self._build_checker(next_iteration)

        if self.iteration == 0:
            source_task_id, source_output_id = self.id, self._fanout_marker_id
        else:
            source_task_id = f"{self.loop_id}#{self.iteration}"
            source_output_id = self.signal_output

        edges = [
            WorkflowEdge(
                source=source_task_id,
                source_output=source_output_id,
                target=body.id,
                target_input=anchor_input,
                transfer=False,
            ),
            WorkflowEdge(
                source=body.id,
                source_output=self.signal_output,
                target=checker.id,
                target_input=checker._loop_in_marker_id,  # noqa: SLF001
                transfer=False,
            ),
        ]

        wf.expand(tasks=[body, checker], edges=edges)

        horus_logger.log.debug(
            _("Loop '%(id)s' injected iteration %(n)d.")
            % {"id": self.loop_id, "n": next_iteration}
        )

    async def _read_signal(self, wf: "BaseWorkflow") -> bool:
        """
        Read the ``{"continue": bool}`` sentinel off the body task this
        controller-check instance is downstream of, and return its
        ``continue`` value.

        Raises:
            LoopConfigurationError: If the body task cannot be found, does
                not declare ``signal_output``, has not written it, or wrote
                something that is not well-formed JSON shaped
                ``{"continue": bool}``.
        """
        body_id = f"{self.loop_id}#{self.iteration}"
        body_task = next((t for t in wf.tasks if t.id == body_id), None)
        if body_task is None:
            raise LoopConfigurationError(
                _(
                    "Loop controller '%(id)s' cannot find its body task "
                    "'%(body_id)s'."
                )
                % {"id": self.id, "body_id": body_id}
            )
        artifact = next(
            (a for a in body_task.outputs if a.id == self.signal_output),
            None,
        )
        if artifact is None:
            raise LoopConfigurationError(
                _(
                    "Loop controller '%(id)s' body task '%(body_id)s' does "
                    "not declare signal output '%(signal)s'."
                )
                % {
                    "id": self.id,
                    "body_id": body_id,
                    "signal": self.signal_output,
                }
            )

        target_path = body_task.target.path_on_target(artifact)
        if not await body_task.target.path_exists(target_path):
            raise LoopConfigurationError(
                _(
                    "Loop controller '%(id)s' body task '%(body_id)s' did "
                    "not write its signal output '%(signal)s'."
                )
                % {
                    "id": self.id,
                    "body_id": body_id,
                    "signal": self.signal_output,
                }
            )

        raw = await body_task.target.get_file(target_path)
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LoopConfigurationError(
                _(
                    "Loop controller '%(id)s' body task '%(body_id)s' "
                    "signal output '%(signal)s' is not valid JSON."
                )
                % {
                    "id": self.id,
                    "body_id": body_id,
                    "signal": self.signal_output,
                }
            ) from exc

        if not isinstance(parsed, dict) or not isinstance(
            parsed.get("continue"), bool
        ):
            raise LoopConfigurationError(
                _(
                    "Loop controller '%(id)s' body task '%(body_id)s' "
                    "signal output '%(signal)s' must be a JSON object with "
                    "a boolean 'continue' key."
                )
                % {
                    "id": self.id,
                    "body_id": body_id,
                    "signal": self.signal_output,
                }
            )
        return bool(parsed["continue"])

    async def _build_body_clone(
        self, wf: "BaseWorkflow", orchestrator: BaseTarget, iteration: int
    ) -> tuple[BaseTask, str]:
        """
        Reconstruct a fresh, independent body clone from ``body_template``
        for *iteration*, pin its declared outputs to iteration-unique paths
        (the template is reused verbatim across iterations, so without this
        every iteration would declare the exact same output path and, from
        the second iteration on, appear already complete and be skipped),
        and materialize the iteration-index anchor input the ordering edge
        into this clone targets.

        Returns ``(clone, anchor_input_id)``: ``anchor_input_id`` is
        ``index_input`` when the caller declared one (its content doubles as
        the real per-iteration index the body reads), or an internal
        never-referenced input synthesized for this purpose otherwise. It is
        always materialized with real content — never left to a
        never-written marker like :class:`.MapExpander`'s fanout marker —
        because, unlike :class:`LoopController` (whose ``_run`` is fully
        overridden), a body clone is an ordinary
        :class:`~horus_builtin.task.horus_task.HorusTask`, whose own
        ``_run`` raises unless every declared input already exists.
        """
        data: dict[str, Any] = {**self.body_template}
        data.setdefault("kind", "horus_task")
        clone_id = f"{self.loop_id}#{iteration}"
        data["id"] = clone_id
        data["name"] = clone_id
        clone = BaseTask.model_validate(data)
        clone.target = clone.target.model_copy(deep=True)

        if not any(o.id == self.signal_output for o in clone.outputs):
            raise LoopConfigurationError(
                _(
                    "Loop task '%(id)s' body template must declare an "
                    "output '%(signal)s' to write the "
                    "{'continue': bool} predicate."
                )
                % {"id": self.id, "signal": self.signal_output}
            )

        body_root = wf.run_directory / f"{self.loop_id}.iter" / str(iteration)
        for artifact in clone.outputs:
            self._pin_path(artifact, body_root / artifact.id)

        if self.index_input is not None:
            anchor_input = self.index_input
            anchor_artifact = next(
                (a for a in clone.inputs if a.id == anchor_input), None
            )
            if anchor_artifact is None:
                raise LoopConfigurationError(
                    _(
                        "Loop task '%(id)s' body template does not "
                        "declare an input '%(input_id)s'."
                    )
                    % {"id": self.id, "input_id": anchor_input}
                )
        else:
            anchor_input = f"{clone_id}{_LOOP_IN_SUFFIX}"
            anchor_artifact = FileArtifact(
                id=anchor_input, path=Path(f"{anchor_input}.json")
            )
            clone.inputs.append(anchor_artifact)

        index_path = body_root / f"{anchor_input}.json"
        await orchestrator.put_file(
            json.dumps(iteration - 1).encode("utf-8"), str(index_path)
        )
        self._pin_path(anchor_artifact, index_path)

        return clone, anchor_input

    def _build_checker(self, iteration: int) -> "LoopController":
        """Build the controller-check instance dispatched after body
        ``f"{loop_id}#{iteration}"`` completes.
        """
        checker_id = f"{self.loop_id}~{iteration}"
        # Rebuild the target from a JSON dump rather than model_copy: while
        # this controller runs, its own target is bound to the live task
        # (an un-pickleable asyncio Task in its private attrs), so a deep
        # copy would fail. A JSON round-trip strips that transient state,
        # yielding a fresh, unbound target of the same kind/config.
        target = BaseTarget.model_validate(self.target.model_dump(mode="json"))
        return LoopController(
            id=checker_id,
            name=checker_id,
            target=target,
            loop_id=self.loop_id,
            iteration=iteration,
            body_template=self.body_template,
            signal_output=self.signal_output,
            max_iterations=self.max_iterations,
            index_input=self.index_input,
        )

    @staticmethod
    def _pin_path(artifact: BaseArtifact, path: Path) -> None:
        """
        Point *artifact* at the absolute *path* and mark it as already
        anchored, so the workflow's run-directory anchoring
        (``BaseWorkflow._anchor_task``, run by ``expand()`` for each new
        task) leaves it untouched.
        """
        artifact.path = path
        artifact.declared_path = path


def lower_loop_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """
    Lower one raw YAML task-dict carrying a ``loop:`` block into a
    ``loop_controller`` task-dict.

    Unlike :func:`~horus_builtin.workflow.map.lower_map_entry`, this needs
    no construction-time edges: a loop has no upstream source collection to
    gate on, so the original controller is typically used directly as the
    run's trigger (exactly like a range-mode ``map:``).

    Args:
        entry: The raw task dict as parsed from YAML, carrying ``id`` and a
            ``loop`` key (``body``, ``until``, ``max_iterations``,
            ``index_input``).

    Returns:
        A ``kind: loop_controller`` task dict ready for
        ``BaseTask.model_validate``.
    """
    task_id = entry["id"]
    loop_block = entry["loop"]

    controller: dict[str, Any] = {
        "kind": "loop_controller",
        "id": task_id,
        "name": entry.get("name") or task_id,
        "description": entry.get("description", ""),
        "loop_id": task_id,
        "body_template": loop_block["body"],
        "signal_output": loop_block["until"],
        "max_iterations": loop_block["max_iterations"],
        "index_input": loop_block.get("index_input"),
        "iteration": 0,
    }
    if entry.get("target") is not None:
        controller["target"] = entry["target"]

    return controller


def loop_task(
    wf: "BaseWorkflow",
    *,
    id: str,
    body: BaseTask,
    until: str,
    max_iterations: int,
    index_input: str | None = None,
    name: str | None = None,
    target: BaseTarget | None = None,
) -> LoopController:
    """
    Append a declarative conditional-repeat loop task to *wf*.

    Mirrors :func:`~horus_builtin.workflow.map.map_task`'s ergonomics for
    the loop construct: builds and appends the same :class:`LoopController`
    that YAML's ``loop:`` block lowers to via :func:`lower_loop_entry`, so
    the two authoring paths produce structurally equivalent tasks.

    Args:
        wf: The workflow to append to.
        id: Id of the loop's original controller task (and id-prefix of
            every injected body/controller-check instance).
        body: The per-iteration template task, in full. Its own
            ``id``/``name`` are ignored — every iteration gets its own
            deterministic id. Must declare an output matching *until* and,
            when relevant, an input matching *index_input*.
        until: Output id on *body* the body task writes each iteration as
            ``{"continue": bool}`` — the loop's predicate. See the module
            docstring's "Predicate convention" section.
        max_iterations: Hard cap on the total number of body iterations.
        index_input: Input id on *body* that receives the iteration's
            0-based integer index. Optional.
        name: Display name; defaults to *id*.
        target: Target the controller instances run on; defaults to
            ``LocalTarget()``.

    Returns:
        The appended :class:`LoopController`.
    """
    body_data = body.model_dump(mode="json")

    kwargs: dict[str, Any] = {
        "id": id,
        "name": name or id,
        "loop_id": id,
        "body_template": body_data,
        "signal_output": until,
        "max_iterations": max_iterations,
        "index_input": index_input,
        "iteration": 0,
    }
    if target is not None:
        kwargs["target"] = target

    controller = LoopController(**kwargs)

    wf.tasks.append(controller)
    return controller
