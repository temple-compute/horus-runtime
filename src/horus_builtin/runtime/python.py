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
Python runtime implementation for in-memory workflows.
"""

from collections.abc import Awaitable, Callable
from importlib import import_module
from inspect import Parameter, signature
from typing import Annotated, Any, ClassVar, cast

from pydantic import BeforeValidator, ConfigDict, PlainSerializer

from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.task.base import BaseTask
from horus_runtime.i18n import tr as _

_PythonFunctionReturnType = BaseArtifact | list[BaseArtifact] | None
PythonFunctionReturnType = (
    _PythonFunctionReturnType | Awaitable[_PythonFunctionReturnType]
)

PythonFunctionSetupTuple = tuple[
    Callable[..., PythonFunctionReturnType],
    dict[str, Any],
]


def import_callable(reference: str) -> Callable[..., Any]:
    """
    Import the callable named by a dotted *reference*.

    Accepts ``package.module:function`` (preferred, unambiguous) and
    ``package.module.function``; for the latter the last dotted component is
    taken as the attribute, and attribute chains
    (``module:Class.method``) work on either side.

    Security note: importing by name runs the target module's top-level code,
    so a workflow file can execute arbitrary Python. That is not a new
    privilege (the same file can already declare a ``command`` runtime or a
    ``python_string`` one, both of which run whatever they are given), so a
    workflow document is trusted input either way and an import allowlist here
    would only give the illusion of a boundary. The guardrail that does pay
    for itself is a legible error: a typo must say which module or attribute
    was missing instead of surfacing a bare ``ImportError`` from pydantic.

    Raises:
        ValueError: If *reference* is malformed, the module cannot be
            imported, the attribute does not exist, or it is not callable.
    """
    module_name, separator, attribute = reference.partition(":")
    if not separator:
        module_name, _sep, attribute = reference.rpartition(".")
    if not module_name or not attribute:
        raise ValueError(
            _(
                "Invalid function reference %(ref)r: expected "
                "'package.module:function' or 'package.module.function'."
            )
            % {"ref": reference}
        )

    try:
        obj: Any = import_module(module_name)
    except ImportError as exc:
        raise ValueError(
            _(
                "Could not import module %(module)r for function reference "
                "%(ref)r: %(err)s"
            )
            % {"module": module_name, "ref": reference, "err": exc}
        ) from exc

    for part in attribute.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise ValueError(
                _(
                    "Module %(module)r has no attribute %(attr)r "
                    "(from function reference %(ref)r)."
                )
                % {
                    "module": module_name,
                    "attr": attribute,
                    "ref": reference,
                }
            ) from exc

    if not callable(obj):
        raise ValueError(
            _("Function reference %(ref)r resolved to a non-callable object.")
            % {"ref": reference}
        )
    return cast(Callable[..., Any], obj)


def _resolve_func(value: Any) -> Any:
    """
    Turn a dotted-path string into the callable it names, pass anything else
    through untouched so handing over a real function keeps working.
    """
    return import_callable(value) if isinstance(value, str) else value


def _serialize_func(func: Callable[..., Any]) -> str:
    """
    Dump a callable as the dotted path that imports it back.

    A callable with no importable name (a lambda, a closure, a
    ``functools.partial``) has no honest answer here; ``repr`` is emitted so
    the dump *fails loudly on load* instead of silently omitting the field and
    failing with "func is required", which said nothing about the cause.
    """
    module = getattr(func, "__module__", None)
    qualname = getattr(func, "__qualname__", None)
    if module and qualname:
        return f"{module}:{qualname}"
    return repr(func)


FunctionReference = Annotated[
    Callable[..., PythonFunctionReturnType],
    BeforeValidator(_resolve_func),
    PlainSerializer(_serialize_func, return_type=str),
]
"""
A callable, or the dotted path of one. Without the string form a function
task cannot be written in YAML at all (a pydantic ``Callable`` field rejects
strings), which kept them out of every tool that goes through the document:
packaging, sanitising, import.
"""


class PythonFunctionRuntime(BaseRuntime[PythonFunctionSetupTuple]):
    """
    Executes a python function.
    """

    kind: str = "python_function"
    kind_name: ClassVar[str] = "Python Function"
    kind_description: ClassVar[str] = _(
        "Executes a Python function in-memory."
    )

    # Allow callable types in the runtime configuration
    model_config = ConfigDict(arbitrary_types_allowed=True)

    func: FunctionReference
    """
    The function to run: either the callable itself (Python workflows) or a
    ``package.module:function`` string (YAML workflows).
    """

    async def _setup_runtime(
        self, task: "BaseTask"
    ) -> PythonFunctionSetupTuple:
        """
        Prepares the runtime for execution by inspecting the function signature
        and collecting arguments from the task's inputs, outputs, and
        variables.

        Arguments:
          task: The task for which the runtime is being set up.

        Raises:
          `ValueError` if the function requires parameters not provided by the
          task.
        """
        # Get the function signature (args and kwargs)
        sig = signature(self.func)

        inputs = {artifact.id: artifact for artifact in task.inputs}
        outputs = {artifact.id: artifact for artifact in task.outputs}

        # Define the allowed parameter names for the function:
        # inputs and outputs
        kwargs: dict[str, BaseArtifact | BaseTask] = {
            **inputs,
            **outputs,
        }

        # Verify that there is no argument that will override the "task"
        # parameter
        if "task" in sig.parameters and "task" in kwargs:
            raise ValueError(
                _(
                    "Function %(func)s has a 'task' parameter that conflicts "
                    "with the task context. Please rename the parameter or "
                    "avoid providing a 'task' variable."
                )
                % {"func": self.func}
            )

        # Add the task itself to the kwargs so it can be injected if the
        # function accepts a "task" parameter.
        kwargs["task"] = task

        # Check that all parameters in the function signature are accounted for
        accepts_kwargs = any(
            param.kind is Parameter.VAR_KEYWORD  # literally '**kwargs'
            for param in sig.parameters.values()
        )

        # If the function accepts **kwargs, we can pass all available kwargs.
        # Otherwise, we filter to only the parameters explicitly defined in the
        # function signature.
        if accepts_kwargs:
            call_kwargs = kwargs
        else:
            # Check that the function signature parameters are a subset of the
            # available kwargs.
            missing_params = set(sig.parameters) - set(kwargs)
            if missing_params:
                raise ValueError(
                    _(
                        "Function %(func)s is missing required parameters: "
                        "%(missing_params)s"
                    )
                    % {"func": self.func, "missing_params": missing_params}
                )

            # Only pass the kwargs that match the function signature
            # parameters.
            call_kwargs = {
                name: value
                for name, value in kwargs.items()
                if name in sig.parameters
            }

        return self.func, call_kwargs
