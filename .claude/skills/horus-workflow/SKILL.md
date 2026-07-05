---
name: horus-workflow
description: >-
  Author, run, and debug horus-runtime workflows in YAML or Python. Use whenever
  the task is to write or edit a workflow definition, wire tasks together with
  edges into a DAG, declare input/output artifacts, choose a runtime/executor/
  target for a task, run a workflow (`horus run`, render_workflow, or
  `wf.run(...)`), define tasks with the @FunctionTask.task decorator, or
  understand execution order, incremental skipping, and artifact transfer.
  Triggers: "horus workflow", "write/build a workflow", "workflow yaml",
  "horus run", "DAG / edges / tasks", "FunctionTask", "run a Horus pipeline".
  For adding a NEW target/runtime/executor/artifact kind, use the horus-plugin
  skill instead.
---

# Authoring horus-runtime workflows

A Horus workflow is a DAG of **tasks** that exchange file-backed **artifacts**.
You can write one declaratively in **YAML** or imperatively in **Python** — both
produce the same object model and run the same way. This skill covers both. To
add a *new capability* (a custom target, runtime, artifact, etc.) use the
companion `horus-plugin` skill.

## Mental model

- A **task** is one unit of work. It binds four things: a **runtime** (*what* to
  run), an **executor** (*how* to run it), a **target** (*where* it runs), and
  **artifacts** (its `inputs` and `outputs`).
- **Edges** connect one task's output artifact to another task's input artifact.
  Edges are the **sole source of truth for the DAG** and for where inputs are
  transferred from. No edges ⇒ tasks are independent (no ordering).
- **Artifact existence is completion.** A task is skipped when
  `skip_if_complete` is true (default) and all its declared `outputs` already
  exist. This gives free incremental re-runs: only missing outputs re-execute. A
  task with **no** outputs always runs.
- A **trigger** task starts a run; the executed scope is the trigger's ancestors
  ∪ descendants, in topological order. Unrelated branches are skipped.

## Two ways to author

**YAML** — declarative, the default for `horus run`. Every nested object carries
a `kind` discriminator that selects the concrete plugin (`kind: command`,
`kind: shell`, `kind: local`, …). Best for reproducible, shareable pipelines of
shell commands / scripts. See `references/yaml-schema.md`.

**Python** — imperative, in-memory. Construct `HorusWorkflow(...)` directly, or
use the `@FunctionTask.task(wf, ...)` decorator to turn a Python function into a
task. Required when a step is an in-memory Python callable rather than a command.
See `references/python-workflows.md`.

## Running a workflow

**CLI (YAML only):**

```bash
horus run WF.yaml                 # trigger = first task; live TUI
horus run WF.yaml --trigger my_task_id
horus run WF.yaml --no-tui        # stream logs instead of the TUI
```

`horus run` boots the runtime, loads via `BaseWorkflow.from_yaml`, and executes
from the trigger downstream. Exit is non-zero on failure.

**Python:** boot the context yourself, then either render the live TUI or run raw:

```python
from horus_runtime.context import HorusContext
from horus_builtin.tui import render_workflow

ctx = HorusContext.boot()
try:
    render_workflow(wf, trigger_id="my_task_id")   # live TUI
    # or, headless:  import asyncio; asyncio.run(wf.run(trigger_id="my_task_id"))
finally:
    ctx.shutdown()
```

`HorusContext.boot()` must run before constructing or loading any workflow (it
loads the plugin registries). Only one workflow may run at a time.

## Built-in `kind` cheat-sheet

| Layer    | Available `kind`s                                            |
| -------- | ----------------------------------------------------------- |
| workflow | `horus_workflow`                                            |
| task     | `horus_task`, `function_task` (Python-only)                 |
| runtime  | `command`, `python`, `python_string`, `python_script`       |
| executor | `shell`, `python_exec`, `python_fn`                         |
| artifact | `file`, `folder`, `json`, `pickle`                          |
| target   | `local`                                                     |

Compatible pairings: `command` runtime ↔ `shell` executor; `python`/string code
↔ `python_exec`; in-memory function ↔ `python_fn`. Mismatches raise
`IncompatibleRuntimeError` at construction.

## Gotchas

- **Load through the base class:** `BaseWorkflow.from_yaml(path)`, not
  `HorusWorkflow.from_yaml(...)`. The `kind` discriminator picks the concrete
  workflow; loading through a concrete subclass defeats the registry (this was a
  real CLI bug fix).
- **Edges are mandatory for ordering.** Two tasks with a data dependency but no
  edge run in undefined order and inputs won't be routed. Wire every dependency.
- **Artifact ids:** unique among root artifacts; unique *within* each task's
  inputs and among its outputs (may repeat across tasks). Edge endpoints resolve
  on `(task_id, artifact_id)`.
- **The id `task` is reserved** in command substitution (`${task.name}`); don't
  name an artifact `task`.
- **`print()` is banned** by the repo's ruff config (T201). In Python function
  tasks, write results to output artifacts; stdout you do emit is captured into a
  per-task `.log` side artifact.
- **Name pattern:** workflow `name` must match `^[a-zA-Z0-9 _-]+$` and be
  non-empty.

## References & templates

- `references/yaml-schema.md` — every field of workflow/task/edge/resources +
  `$`-substitution, with complete YAML examples.
- `references/python-workflows.md` — direct construction and the
  `@FunctionTask.task` decorator, with runnable examples.
- `references/execution.md` — DAG scoping, topological order, incremental skip,
  transfer, and the target dispatch lifecycle.
- `assets/workflow_example.yaml` — a producer→consumer workflow to adapt.
- `assets/workflow_example.py` — direct-construction and decorator examples,
  runnable via `python`.
