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

import pytest

from horus_runtime.core.task.exceptions import (
    TaskExecutionError,
    summarize_exception,
)


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
