# Python workflows

Build workflows in-memory. Two styles: **direct construction** (assemble
`HorusWorkflow` from task objects + edges) and the **`@FunctionTask.task`
decorator** (register a Python function as a task). Use Python when a step is an
in-memory callable, or when you're generating a workflow programmatically.

Always `HorusContext.boot()` before constructing tasks/workflows (it loads the
plugin registries) and `ctx.shutdown()` when done. Run with `render_workflow`
(live TUI) or `asyncio.run(wf.run(trigger_id=...))` (headless).

## Direct construction

Import the concrete built-ins and assemble them. The building blocks:

```python
from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.core.workflow.edge import WorkflowEdge

producer = HorusTask(
    id="producer",
    name="producer",
    outputs=[FileArtifact(id="data", path="/tmp/horus_demo/data.txt")],
    runtime=CommandRuntime(command="echo 42 > $data"),
    executor=ShellExecutor(),
    target=LocalTarget(),
)
consumer = HorusTask(
    id="consumer",
    name="consumer",
    inputs=[FileArtifact(id="data_in", path="/tmp/horus_demo/data.txt")],
    outputs=[FileArtifact(id="out", path="/tmp/horus_demo/out.txt")],
    runtime=CommandRuntime(command="cat ${data_in} > $out"),
    executor=ShellExecutor(),
    target=LocalTarget(),
)
wf = HorusWorkflow(
    name="order_demo",
    tasks=[consumer, producer],          # order in the list doesn't matter
    edges=[
        WorkflowEdge(
            source="producer",
            source_output="data",
            target="consumer",
            target_input="data_in",
        )
    ],
)
# await wf.run(trigger_id="producer")    # topological: producer then consumer
```

The edge — not list order — determines execution order. `HorusTask.target`
defaults to `LocalTarget()` if omitted.

## The `@FunctionTask.task` decorator

`FunctionTask` (`horus_builtin/task/function.py`, `kind="function_task"`) wraps a
Python callable. The decorator appends a task to a workflow:

```python
from horus_builtin.artifact.file import FileArtifact
from horus_builtin.task.function import FunctionTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow

wf = HorusWorkflow(name="fn_demo")

@FunctionTask.task(
    wf,
    inputs=[FileArtifact(id="src", path="/tmp/horus_demo/in.txt")],
    outputs=[FileArtifact(id="dst", path="/tmp/horus_demo/out.txt")],
)
def upcase(src: FileArtifact, dst: FileArtifact) -> None:
    """Uppercase the input file into the output file."""
    dst.path.write_text(src.path.read_text().upper())
```

Decorator signature:
`FunctionTask.task(wf, *, id=None, name=None, inputs=None, outputs=None, target=None)`.
`id`/`name` default to the function name; `target` defaults to `LocalTarget()`.

### Argument injection

The `python_function` runtime inspects the function signature and injects
arguments **by parameter name**:

- a parameter named after an input or output artifact id receives that artifact;
- a parameter named `task` receives the `BaseTask` itself (don't also name an
  artifact `task`);
- if the function declares `**kwargs`, it receives all of the above;
- a parameter matching nothing raises `ValueError` at setup.

Functions may be **sync or async**; async functions are awaited. They run in the
task's working directory (relative paths resolve there, matching `ShellExecutor`).

### Returning side-artifacts

A function may **return** a `BaseArtifact` or `list[BaseArtifact]` to register
side-artifacts (inspectable extras like logs/plots) on `task.side_artifacts`.
Return `None` (the common case) when the declared outputs are all you need. Any
other return value is ignored with a warning.

```python
@FunctionTask.task(wf, outputs=[FileArtifact(id="report", path="/tmp/r.txt")])
def make_report(report: FileArtifact) -> FileArtifact:
    """Write the report and expose a log as a side-artifact."""
    report.path.write_text("done")
    log = FileArtifact(id="run_log", path="/tmp/run.log")
    log.path.write_text("all good")
    return log
```

### Logging (no `print`)

`print()` is banned by the repo's ruff config. Use loguru; stdout that a task does
emit is captured into a per-task `.log` side artifact by the log-file middleware.

```python
from horus_runtime.logging import horus_logger
horus_logger.log.info("processing %s", src.path)
```

## Full runnable skeleton

```python
import asyncio
from horus_runtime.context import HorusContext
from horus_builtin.tui import render_workflow

def build() -> "HorusWorkflow":
    ...   # construct wf as above

def main() -> None:
    ctx = HorusContext.boot()
    try:
        wf = build()
        render_workflow(wf, trigger_id="producer")
        # headless alternative:
        # asyncio.run(wf.run(trigger_id="producer"))
    finally:
        ctx.shutdown()

if __name__ == "__main__":
    main()
```

See `assets/workflow_example.py` for a complete, lint-clean version.
