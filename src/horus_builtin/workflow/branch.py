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
Switch-style branching: one function picks which downstream path(s) run.

:mod:`horus_builtin.workflow.condition` already supports branching by putting
a predicate on each outgoing edge. That is the right model for the canvas and
for YAML, but it is an awkward way to write "pick one of these three" in
Python: the author has to spread one decision across N mutually exclusive
predicates and keep them consistent by hand. A :class:`BranchRouter` is the
same decision written once, as a function returning the id(s) to take.

Lowering, not a second evaluation path
---------------------------------------
The load-bearing design decision: a router **lowers to** the declarative form
rather than teaching the scheduler a new trick. :meth:`BranchRouter._run`
calls the function, validates what it returned against the declared
``routes``, and writes a JSON sentinel shaped ``{"routes": [...]}``. Every
outgoing edge is then an ordinary artifact-less ordering edge carrying an
ordinary :class:`~horus_runtime.core.workflow.condition.EdgeCondition`
(``key="routes"``, ``op="contains"``, ``value=<that route's task id>``) that
reads the sentinel, exactly as if the user had hand-written it.

So a router is *authoring sugar over data*, and everything downstream of the
decision, the liveness rule
(:func:`horus_builtin.workflow.condition.compute_liveness`), the scheduler's
skip gate, the YAML round-trip, and the future canvas, needs zero extra cases:
they only ever see edges with declarative conditions. The one thing a router
adds is a task that computes the sentinel, and computing a JSON output is what
every task already does.

The consequence worth stating: a workflow whose router function cannot be
re-imported (a lambda, or a module absent from the run's environment) still
*routes* correctly wherever the function ran, because the decision has already
been reduced to data by the time any edge is evaluated. Only re-deriving the
decision needs the callable.

Why the sentinel is always re-derived
---------------------------------------
:meth:`BranchRouter.is_complete` is permanently ``False``, mirroring
:class:`~horus_builtin.workflow.map.MapExpander`: a stale sentinel from an
earlier run would silently pin a resumed run to the old branch, so the router
re-runs and re-decides deterministically. The tasks on the branches it selects
are ordinary tasks and are still skipped individually by the usual
``skip_if_complete`` behaviour, so re-deciding is cheap.
"""

import json
from collections.abc import Callable
from inspect import signature
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import ConfigDict, Field, model_validator

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.condition import _resolve_ref
from horus_runtime.core.artifact.store import ArtifactStore
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.workflow.condition import EdgeCondition, derive_ref
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

if TYPE_CHECKING:
    from horus_runtime.core.workflow.base import BaseWorkflow

_ROUTES_SUFFIX = ".routes"
_ROUTES_KEY = "routes"


class BranchConfigurationError(WorkflowError):
    """Raised when a ``wf.branch(...)`` call, or the function behind it, is
    malformed.
    """


class BranchRouter(HorusTask):
    """
    Picks which downstream path(s) run by calling a Python function.

    Writes its decision as a ``{"routes": [...]}`` JSON sentinel, which its
    outgoing edges gate on declaratively. See the module docstring for why the
    router lowers to the declarative form instead of being evaluated specially.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: str = "branch_router"
    kind_name: ClassVar[str] = "Branch Router"
    kind_description: ClassVar[str] = _(
        "Calls a Python function to pick which of its declared routes run, "
        "and records the choice as a JSON sentinel its outgoing edges gate on."
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

    routes: list[str]
    """
    Candidate target task ids, the closed set the function may return from.
    Declared rather than inferred from the edges so a decision can be checked
    against it before any edge is looked at, and so the canvas can draw the
    branch's arms before the router has ever run.
    """

    func: Callable[..., str | list[str]] | None = Field(
        default=None, exclude=True
    )
    """
    The in-memory decision function, returning one route id or a list of them
    (an empty list is legal and takes no branch at all). Optionally declares a
    ``task`` parameter to receive this router, mirroring how
    :class:`~horus_builtin.runtime.python.PythonFunctionRuntime` injects it.

    Excluded from serialization, exactly as ``PythonCondition.func`` is: a
    function cannot be written to YAML, and the backend stores workflows as
    opaque JSON.
    """

    ref: str | None = None
    """
    ``module:qualname`` of ``func``, derived automatically via
    :func:`~horus_runtime.core.workflow.condition.derive_ref`. This is what
    survives ``model_dump``, so it is how a run dispatched to the orchestrator
    recovers the decision function.
    """

    @property
    def routes_output_id(self) -> str:
        """Id of the JSON sentinel output holding this router's decision."""
        return f"{self.id}{_ROUTES_SUFFIX}"

    @model_validator(mode="after")
    def _ensure_routes_output(self) -> Self:
        """
        Append the sentinel output if not already present (idempotent, so
        re-loading an already-dumped router does not duplicate it).

        Unlike :class:`~horus_builtin.workflow.map.MapExpander`'s wiring
        marker, this one is really written: it is the whole interface between
        the router and the declarative conditions on its outgoing edges.
        """
        output_id = self.routes_output_id
        if not any(o.id == output_id for o in self.outputs):
            self.outputs.append(
                FileArtifact(id=output_id, path=Path(f"{output_id}.json"))
            )
        return self

    @model_validator(mode="after")
    def _derive_ref(self) -> Self:
        """Fill ``ref`` from ``func`` so the dump stays meaningful."""
        if self.ref is None:
            self.ref = derive_ref(self.func)
        return self

    @model_validator(mode="after")
    def _check_resolvable(self) -> Self:
        """A router with neither a callable nor a reference can never
        decide.
        """
        if self.func is None and self.ref is None:
            raise ValueError(
                _(
                    "A branch router needs either a callable or a "
                    "'module:qualname' reference."
                )
            )
        return self

    async def is_complete(self) -> bool:
        """
        Always incomplete: the router must re-run on every trigger to
        re-derive its decision. A sentinel left over from an earlier run
        would otherwise pin a resumed run to the branch it took last time.
        """
        return False

    async def _reset(self) -> None:
        """
        Delete the sentinel, mirroring
        :meth:`~horus_builtin.task.horus_task.HorusTask._reset`. The tasks on
        the routes are ordinary tasks and reset independently.
        """
        store = ArtifactStore(self.target)
        for artifact in self.outputs:
            await store.delete(artifact)
        self.runs = 0

    async def _run(self) -> None:
        """
        Call the decision function, validate what it returned, and write the
        ``{"routes": [...]}`` sentinel its outgoing edges gate on.
        """
        self.runs += 1

        chosen = self._normalize(await self._decide())

        unknown = [route for route in chosen if route not in self.routes]
        if unknown:
            raise BranchConfigurationError(
                _(
                    "Branch router '%(id)s' chose route(s) %(unknown)s, which "
                    "are not among its declared routes %(routes)s."
                )
                % {
                    "id": self.id,
                    "unknown": ", ".join(repr(r) for r in unknown),
                    "routes": ", ".join(repr(r) for r in self.routes),
                }
            )

        await self._write_sentinel(chosen)

        horus_logger.log.debug(
            _("Branch router '%(id)s' chose %(chosen)s.")
            % {"id": self.id, "chosen": chosen or "no route"}
        )

    async def _decide(self) -> Any:
        """
        Resolve and invoke the decision function.

        The in-memory callable wins over ``ref``: a router built in this
        process holds the real function, and re-importing it would be both
        pointless and wrong for a closure that never had an importable name.
        """
        func = self.func
        if func is None:
            if self.ref is None:
                raise BranchConfigurationError(
                    _(
                        "Branch router '%(id)s' has neither a callable nor a "
                        "reference."
                    )
                    % {"id": self.id}
                )
            func = _resolve_ref(self.ref)

        result = func(**self._call_kwargs(func))
        # Async deciders are supported for the same reason evaluate_condition
        # supports them: a decision may need to read something remote.
        if hasattr(result, "__await__"):
            result = await result
        return result

    def _call_kwargs(self, func: Callable[..., Any]) -> dict[str, Any]:
        """
        Inject this router as ``task`` if, and only if, the function asks for
        it by name, following
        :class:`~horus_builtin.runtime.python.PythonFunctionRuntime`'s
        convention. A self-contained decider stays a zero-argument function.
        """
        try:
            parameters = signature(func).parameters
        except (TypeError, ValueError):
            # A builtin or C-implemented callable exposes no signature; it
            # cannot be asking for a task parameter either.
            return {}
        return {"task": self} if "task" in parameters else {}

    def _normalize(self, result: Any) -> list[str]:
        """
        Accept a single route id or a list of them, and reject anything else.

        Returning a bare string is the common case ("take this branch"), so
        requiring a one-element list for it would be noise.
        """
        if isinstance(result, str):
            return [result]
        if isinstance(result, (list, tuple)) and all(
            isinstance(item, str) for item in result
        ):
            return list(result)
        raise BranchConfigurationError(
            _(
                "Branch router '%(id)s' function must return a route id or a "
                "list of route ids, got %(result)r."
            )
            % {"id": self.id, "result": result}
        )

    async def _write_sentinel(self, chosen: list[str]) -> None:
        """Write the decision as ``{"routes": [...]}`` on this router's own
        target, where the edge conditions will read it back from.
        """
        artifact = next(
            (a for a in self.outputs if a.id == self.routes_output_id), None
        )
        if artifact is None:  # pragma: no cover - the validator guarantees it
            raise BranchConfigurationError(
                _("Branch router '%(id)s' lost its sentinel output.")
                % {"id": self.id}
            )
        path = self.target.path_on_target(artifact)
        await self.target.mkdir(str(Path(path).parent))
        await self.target.put_file(
            json.dumps({_ROUTES_KEY: chosen}).encode("utf-8"), path
        )


def route_edge(
    router_id: str, routes_output_id: str, route: str
) -> WorkflowEdge:
    """
    Build the one edge that gates *route* on *router_id*'s sentinel.

    Deliberately the only place a router's wiring is expressed, so "what a
    router lowers to" has a single definition that
    :func:`branch_task` uses and tests can compare a hand-written declarative
    branch against.

    The edge is artifact-less (hence ordering-only): the branch target depends
    on the *decision*, not on the sentinel's bytes, so it needs no input to
    receive them into. That in turn is why the condition names its own source
    rather than inheriting the edge's endpoints.

    Args:
        router_id: Id of the :class:`BranchRouter`.
        routes_output_id: Id of the router's sentinel output.
        route: Target task id this edge gates.

    Returns:
        The gated :class:`~horus_runtime.core.workflow.edge.WorkflowEdge`.
    """
    return WorkflowEdge(
        source=router_id,
        target=route,
        condition=EdgeCondition(
            source_task=router_id,
            source_output=routes_output_id,
            key=_ROUTES_KEY,
            op="contains",
            value=route,
        ),
    )


def branch_task(
    wf: "BaseWorkflow",
    *,
    id: str,
    func: Callable[..., str | list[str]],
    routes: list[str],
    name: str | None = None,
    target: BaseTarget | None = None,
) -> BranchRouter:
    """
    Append a switch-style branch to *wf*: a router task plus one gated edge
    per route.

    Mirrors :func:`~horus_builtin.workflow.map.map_task`'s ergonomics. Unlike
    map and loop there is no YAML block to lower from, because the declarative
    form a router lowers *to* is already the YAML authoring form: hand-write
    the conditions on the edges and no router is needed at all.

    Args:
        wf: The workflow to append to.
        id: Id of the router task.
        func: The decision function, returning one route id or a list of them.
            May declare a ``task`` parameter to receive the router.
        routes: Candidate target task ids. Each must already name a task on
            *wf*, since each gets a gated edge from the router.
        name: Display name; defaults to *id*.
        target: Target the router itself runs on; defaults to
            ``LocalTarget()``.

    Returns:
        The appended :class:`BranchRouter`.

    Raises:
        BranchConfigurationError: If *routes* is empty, contains duplicates,
            or names a task that does not exist on *wf*.
    """
    if not routes:
        raise BranchConfigurationError(
            _("wf.branch(id='%(id)s', ...) requires at least one route.")
            % {"id": id}
        )
    if len(set(routes)) != len(routes):
        raise BranchConfigurationError(
            _("wf.branch(id='%(id)s', ...) has duplicate routes.") % {"id": id}
        )

    known = {task.id for task in wf.tasks}
    unknown = [route for route in routes if route not in known]
    if unknown:
        raise BranchConfigurationError(
            _(
                "wf.branch(id='%(id)s', ...) routes to unknown task(s) "
                "%(unknown)s. Add the branch targets to the workflow first."
            )
            % {"id": id, "unknown": ", ".join(repr(r) for r in unknown)}
        )

    kwargs: dict[str, Any] = {
        "id": id,
        "name": name or id,
        "func": func,
        "routes": routes,
    }
    if target is not None:
        kwargs["target"] = target

    router = BranchRouter(**kwargs)

    wf.tasks.append(router)
    wf.edges.extend(
        route_edge(router.id, router.routes_output_id, route)
        for route in routes
    )
    return router
