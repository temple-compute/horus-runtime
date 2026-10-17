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
Exceptions related to task execution in the Horus runtime.
"""


class BaseTaskError(Exception):
    """
    Base exception for task-related errors in the Horus runtime.
    """


class TaskExecutionError(BaseTaskError):
    """
    Exception raised when a task execution fails in the Horus runtime.
    """


class TargetLostError(TaskExecutionError):
    """
    The machine went away under the task; the task itself did not fail.
    Raised by targets. The scheduler retries the task once on a new machine.
    """


class TaskMissingIdError(BaseTaskError):
    """
    Exception raised when a task is about to be executed but has no id
    assigned. This typically means the task was added to the workflow after
    construction (e.g. via a decorator) without explicitly setting id.
    """


def exception_leaves(exc: BaseException) -> list[BaseException]:
    """
    The innermost exceptions of *exc*: itself, or for an ``ExceptionGroup``
    (e.g. from an ``asyncio.TaskGroup``) the leaves of every nested group.
    The group wrapper only says how many sub-exceptions there were; the
    leaves say what actually went wrong.
    """
    if isinstance(exc, BaseExceptionGroup):
        return [
            leaf for sub in exc.exceptions for leaf in exception_leaves(sub)
        ]
    return [exc]


def summarize_exception(exc: BaseException) -> str:
    """
    A one-line ``"<Type>: <first line of message>"`` summary of *exc*,
    describing its first leaf (see :func:`exception_leaves`) and counting
    the rest.
    """
    leaves = exception_leaves(exc)
    first = leaves[0]
    lines = [line.strip() for line in str(first).splitlines() if line.strip()]
    summary = type(first).__name__
    if lines:
        summary += f": {lines[0]}"
    if len(leaves) > 1:
        summary += f" (+{len(leaves) - 1} more)"
    return summary
