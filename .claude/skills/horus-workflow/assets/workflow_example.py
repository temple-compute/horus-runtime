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
Example horus-runtime workflow built in Python.

Shows both authoring styles:

* ``build_command_workflow`` — direct construction from shell-command tasks
  wired with an edge (equivalent to assets/workflow_example.yaml).
* ``build_function_workflow`` — the ``@FunctionTask.task`` decorator turning a
  Python function into a task.

Run with:  python workflow_example.py
"""

from pathlib import Path

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.function import FunctionTask
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.tui import render_workflow
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.logging import horus_logger

WORK_DIR = Path("/tmp/horus_example")


def build_command_workflow() -> HorusWorkflow:
    """
    Producer -> consumer using shell commands and an edge.

    The edge (not list order) makes ``producer`` run before ``consumer`` and
    routes the produced file to the consumer's input.
    """
    producer = HorusTask(
        id="producer",
        name="producer",
        outputs=[FileArtifact(id="data", path=WORK_DIR / "data.txt")],
        runtime=CommandRuntime(
            command="mkdir -p /tmp/horus_example && echo 42 > $data"
        ),
        executor=ShellExecutor(),
        target=LocalTarget(),
    )
    consumer = HorusTask(
        id="consumer",
        name="consumer",
        inputs=[FileArtifact(id="data_in", path=WORK_DIR / "data.txt")],
        outputs=[FileArtifact(id="summary", path=WORK_DIR / "summary.txt")],
        runtime=CommandRuntime(command="wc -l ${data_in} > $summary"),
        executor=ShellExecutor(),
        target=LocalTarget(),
    )
    return HorusWorkflow(
        name="example_pipeline",
        tasks=[consumer, producer],
        edges=[
            WorkflowEdge(
                source="producer",
                source_output="data",
                target="consumer",
                target_input="data_in",
            )
        ],
    )


def build_function_workflow() -> HorusWorkflow:
    """
    A single Python-function task registered with the decorator.

    Parameters are injected by name: ``src`` and ``dst`` match the input and
    output artifact ids.
    """
    wf = HorusWorkflow(name="function_pipeline")

    @FunctionTask.task(
        wf,
        inputs=[FileArtifact(id="src", path=WORK_DIR / "data.txt")],
        outputs=[FileArtifact(id="dst", path=WORK_DIR / "upper.txt")],
    )
    def upcase(src: FileArtifact, dst: FileArtifact) -> None:
        """Uppercase the input file into the output file."""
        dst.path.write_text(src.path.read_text().upper())

    return wf


def main() -> None:
    """Boot the runtime and run the command workflow under the live TUI."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ctx = HorusContext.boot()
    try:
        wf = build_command_workflow()
        render_workflow(wf, trigger_id="producer")
        horus_logger.log.info("workflow finished: %s", wf.status)
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()
