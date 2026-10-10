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
Unit tests for workflow-level ``default_executor``/``default_target``: a
scaffold's HOW/WHERE, filled onto any task whose dict omits the field.
"""

import pytest
from pydantic import ValidationError

from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.workflow.horus_workflow import HorusWorkflow


def _task_dict(task_id: str, **overrides: object) -> dict[str, object]:
    base = {
        "kind": "horus_task",
        "id": task_id,
        "name": task_id,
        "runtime": {"kind": "command", "command": "echo hi"},
    }
    return {**base, **overrides}


def test_task_omitting_executor_and_target_inherits_scaffold() -> None:
    wf = HorusWorkflow.model_validate(
        {
            "kind": "horus_workflow",
            "name": "docking",
            "default_executor": {"kind": "shell"},
            "default_target": {"kind": "local", "working_directory": "/scratch/a"},
            "tasks": [_task_dict("dock_1")],
        }
    )
    task = wf.tasks[0]
    assert isinstance(task.executor, ShellExecutor)
    assert isinstance(task.target, LocalTarget)
    assert task.target.working_directory == "/scratch/a"


def test_task_override_wins_over_scaffold_default() -> None:
    wf = HorusWorkflow.model_validate(
        {
            "kind": "horus_workflow",
            "name": "docking",
            "default_executor": {"kind": "shell"},
            "default_target": {"kind": "local", "working_directory": "/scratch/a"},
            "tasks": [
                _task_dict(
                    "dock_1",
                    executor={"kind": "shell"},
                    target={"kind": "local", "working_directory": "/scratch/b"},
                )
            ],
        }
    )
    assert wf.tasks[0].target.working_directory == "/scratch/b"


def test_no_defaults_and_no_task_executor_still_fails() -> None:
    with pytest.raises(ValidationError):
        HorusWorkflow.model_validate(
            {
                "kind": "horus_workflow",
                "name": "docking",
                "tasks": [_task_dict("dock_1")],
            }
        )


def test_defaults_apply_to_the_lowered_loop_controller() -> None:
    """
    Validator ordering: defaults must be filled in *after* ``loop:`` sugar is
    lowered into a real ``loop_controller`` dict, else ``setdefault`` would
    act on the pre-lowering ``loop:`` block instead of a task dict and no-op.

    Only the controller task itself is covered -- each per-iteration clone is
    reconstructed later, straight from ``body_template``, outside this
    validator's reach.
    """
    wf = HorusWorkflow.model_validate(
        {
            "kind": "horus_workflow",
            "name": "docking",
            "default_executor": {"kind": "shell"},
            "default_target": {"kind": "local", "working_directory": "/scratch/a"},
            "tasks": [
                {
                    "id": "loop_1",
                    "name": "loop_1",
                    "loop": {
                        "body": {
                            "kind": "horus_task",
                            "runtime": {"kind": "command", "command": "echo $item"},
                            "executor": {"kind": "shell"},
                            "target": {"kind": "local"},
                        },
                        "until": "done",
                        "max_iterations": 2,
                    },
                }
            ],
        }
    )
    (loop_task,) = wf.tasks
    assert isinstance(loop_task.executor, ShellExecutor)
    assert loop_task.target.working_directory == "/scratch/a"


def test_no_scaffold_defaults_is_a_no_op() -> None:
    wf = HorusWorkflow.model_validate(
        {
            "kind": "horus_workflow",
            "name": "docking",
            "tasks": [
                _task_dict(
                    "dock_1",
                    executor={"kind": "shell"},
                    target={"kind": "local"},
                )
            ],
        }
    )
    assert wf.default_executor is None
    assert wf.default_target is None
    assert isinstance(wf.tasks[0].executor, ShellExecutor)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
