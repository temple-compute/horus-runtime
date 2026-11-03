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
"""Tests for the task exception helpers."""

import pickle

import pytest

from horus_runtime.core.task.exceptions import (
    CommandFailedError,
    TaskExecutionError,
    summarize_exception,
)
from horus_runtime.core.task.status import SkipReason


@pytest.mark.unit
def test_summary_is_type_and_first_line() -> None:
    """A plain exception becomes ``Type: first line``."""
    exc = TaskExecutionError("sbatch: error: bad limit\nmore detail")
    assert (
        summarize_exception(exc)
        == "TaskExecutionError: sbatch: error: bad limit"
    )


@pytest.mark.unit
def test_summary_of_a_group_names_its_leaves() -> None:
    """A nested ExceptionGroup is summarized by its leaves, not the wrapper."""
    group = ExceptionGroup(
        "unhandled errors in a TaskGroup (1 sub-exception)",
        [
            ExceptionGroup(
                "inner", [TaskExecutionError("first"), ValueError("second")]
            )
        ],
    )
    assert summarize_exception(group) == "TaskExecutionError: first (+1 more)"


@pytest.mark.unit
def test_summary_without_message_is_the_type() -> None:
    """An exception without a message is summarized by its type alone."""
    assert summarize_exception(KeyboardInterrupt()) == "KeyboardInterrupt"


@pytest.mark.unit
def test_command_failed_error_pickles_exit_code() -> None:
    """The exit code and message survive a pickle round trip."""
    exc = CommandFailedError("exited with 3", 3)
    copy = pickle.loads(pickle.dumps(exc))
    assert isinstance(copy, CommandFailedError)
    assert isinstance(copy, TaskExecutionError)
    assert copy.exit_code == 3
    assert str(copy) == "exited with 3"


@pytest.mark.unit
def test_command_failed_error_pickles_notes() -> None:
    """A note added after construction survives a pickle round trip."""
    exc = CommandFailedError("exited with 3", 3)
    exc.add_note("on node-1")
    copy = pickle.loads(pickle.dumps(exc))
    assert copy.__notes__ == ["on node-1"]
    assert copy.exit_code == 3


class _OwnInitError(CommandFailedError):
    """A subclass whose ``__init__`` takes other arguments."""

    def __init__(self, path: str) -> None:
        super().__init__(f"{path} failed", 7)
        self.path = path


@pytest.mark.unit
def test_command_failed_error_subclass_with_own_init_pickles() -> None:
    """A subclass keeps its type, exit code and fields across pickling."""
    copy = pickle.loads(pickle.dumps(_OwnInitError("/w/x")))
    assert type(copy) is _OwnInitError
    assert copy.exit_code == 7
    assert copy.path == "/w/x"
    assert str(copy) == "/w/x failed"


@pytest.mark.unit
def test_drop_skip_reasons_keep_their_values() -> None:
    """The drop reasons are persisted by value, so the values are fixed."""
    assert SkipReason("upstream_dropped") is SkipReason.UPSTREAM_DROPPED
    assert SkipReason("dropped") is SkipReason.DROPPED
