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
from pathlib import Path
from typing import ClassVar, Literal

import yaml

from horus_runtime.core.workflow.base import BaseWorkflow


class HorusWorkflow(BaseWorkflow):
    """
    Basic implementation of the Workflow class for Horus built-in workflows.

    The workflow determines whether each task needs to run by inspecting the
    existence of its declared output artifacts. Tasks that have already
    produced all their outputs are skipped, which provides basic incremental
    execution: re-running a workflow only re-executes tasks whose outputs are
    missing.
    """

    kind: ClassVar[Literal["horus_workflow"]] = "horus_workflow"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "HorusWorkflow":
        with Path(path).open("r", encoding="utf-8") as fh:
            return cls.model_validate(yaml.safe_load(fh))

    def run(self) -> None:
        """
        Tasks are executed in definition order. A task is skipped when all of
        its output artifacts exist (see :meth:`is_complete`).
        """
        for task in self.tasks.values():
            if task.is_complete():
                print(
                    f"[{self.name}] Skipping '{task.name}': "
                    "all outputs already exist."
                )
                continue

            print(f"[{self.name}] Running '{task.name}'...")

            task.run()

            print(f"[{self.name}] '{task.name}' completed.")

    def reset(self) -> None:
        """
        Reset the workflow by deleting all output artifacts of all tasks in the
        workflow. This allows the workflow to be re-run from scratch.
        """
        for task in self.tasks.values():
            print(f"[{self.name}] Resetting task '{task.name}'...")
            task.reset()
