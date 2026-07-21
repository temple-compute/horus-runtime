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
Serializable predicates that gate an edge, letting a workflow branch.

Two forms, both attached to a ``WorkflowEdge``:

``EdgeCondition``
    Declarative data: read a JSON sentinel written by an upstream task, pull a
    key out of it, compare it to a literal. No code, so it round-trips through
    YAML and can be authored and edited in the canvas.

``PythonCondition``
    An arbitrary Python callable, for workflows authored in Python where
    expressing the predicate as data would be clumsy. The callable itself is
    excluded from serialization (as ``PythonFunctionRuntime.func`` already is),
    so a ``module:qualname`` reference and a human-readable label are carried
    alongside it: that is what survives the dump, what the canvas renders, and
    what the orchestrator re-imports.

Both are evaluated by ``horus_builtin.workflow.condition``. The model lives in
core because ``WorkflowEdge`` references it; evaluation lives in builtin
because it reads from targets.
"""

from collections.abc import Callable
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from horus_runtime.i18n import tr as _

ConditionOp = Literal[
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "in",
    "not_in",
    "truthy",
    "exists",
]
"""
The closed set of comparisons an ``EdgeCondition`` may apply. Deliberately
closed: an open expression grammar would be a code-execution surface in a
document the backend stores verbatim and never validates.
"""


class _SourcedCondition(BaseModel):
    """
    Shared field pair: which artifact holds the value being tested.

    Both forms need this, and for the same reason. A branch edge is typically
    artifact-less (the downstream task depends on the *decision*, not on the
    decider's data), so the edge's own endpoints supply no default and the
    condition has to name its sentinel itself.
    """

    source_task: str | None = None
    """
    Task whose output holds the value. Defaults to the edge's own ``source``,
    which is the common case (test the thing you just depended on).
    """

    source_output: str | None = None
    """
    Output artifact id on ``source_task``, holding a JSON document. Defaults to
    the edge's own ``source_output``.
    """


class EdgeCondition(_SourcedCondition):
    """
    A declarative predicate over an upstream task's JSON output.
    """

    kind: Literal["declarative"] = "declarative"
    """Discriminator against ``PythonCondition``."""

    key: str | None = None
    """
    Dotted path into the JSON document, e.g. ``metrics.accuracy``. ``None``
    tests the whole document.
    """

    op: ConditionOp = "truthy"
    """The comparison to apply."""

    value: Any = None
    """
    JSON-safe literal to compare against. Unused by ``truthy`` and ``exists``;
    must be a list for ``in`` / ``not_in``.
    """

    @model_validator(mode="after")
    def check_value_matches_op(self) -> Self:
        """
        Reject operator/value combinations that could never evaluate.

        Caught here rather than at evaluation time because a condition that
        only explodes mid-run has already cost the user everything upstream of
        the branch.
        """
        if self.op in ("in", "not_in") and not isinstance(
            self.value, (list, tuple, set, str)
        ):
            raise ValueError(
                _(
                    "Condition operator '%(op)s' needs a collection to test "
                    "membership against, got %(value)r."
                )
                % {"op": self.op, "value": self.value}
            )
        return self


class PythonCondition(_SourcedCondition):
    """
    A predicate backed by a Python callable, with a serializable reference.

    The callable receives the resolved JSON document of the source output (or
    ``None`` when there is no source, or it wrote nothing) and returns a bool.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: Literal["python"] = "python"
    """Discriminator against ``EdgeCondition``."""

    func: Callable[..., bool] | None = Field(default=None, exclude=True)
    """
    The in-memory callable. Excluded from serialization: a function cannot be
    written to YAML, and the backend stores workflows as opaque JSON. Present
    only within the process that built the workflow.
    """

    ref: str | None = None
    """
    ``module:qualname`` of ``func``, derived automatically when built from a
    callable. This is what survives ``model_dump``, so it is how a run
    dispatched to the orchestrator recovers the predicate. Resolvable only if
    the module is importable in the run's plugin environment.
    """

    label: str | None = None
    """
    Short human-readable summary for display, e.g. ``accuracy > 0.9``. Derived
    from the callable's docstring when not given. The canvas shows this, since
    it cannot render the body of a function it does not have.
    """

    @model_validator(mode="after")
    def derive_ref_and_label(self) -> Self:
        """
        Fill ``ref`` and ``label`` from ``func`` so the dump stays meaningful.

        A lambda has a ``<lambda>`` qualname that cannot be imported back, so
        it gets no ``ref``: it works in-process and fails loudly elsewhere,
        which is better than emitting a reference that silently will not
        resolve.
        """
        if self.func is not None and self.ref is None:
            qualname = getattr(self.func, "__qualname__", "")
            module = getattr(self.func, "__module__", "")
            if module and qualname and "<lambda>" not in qualname:
                self.ref = f"{module}:{qualname}"

        if self.label is None:
            doc = getattr(self.func, "__doc__", None)
            if doc and doc.strip():
                self.label = doc.strip().splitlines()[0].strip()
            elif self.ref:
                self.label = self.ref.rsplit(":", 1)[-1]

        return self

    @model_validator(mode="after")
    def check_resolvable(self) -> Self:
        """
        A condition with neither a callable nor a reference can never evaluate.
        """
        if self.func is None and self.ref is None:
            raise ValueError(
                _(
                    "A Python condition needs either a callable or a "
                    "'module:qualname' reference."
                )
            )
        return self


Condition = EdgeCondition | PythonCondition
"""
Either predicate form. Discriminated on ``kind`` so a dumped condition loads
back as the right class.
"""
