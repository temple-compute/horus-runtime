# Runtime architecture

How the engine is laid out, how it boots, and how a workflow runs. Everything
here is background for writing plugins; file paths are relative to
`src/horus_runtime/` (engine) or `src/horus_builtin/` (built-in plugins).

## Two packages

- **`horus_runtime`** — the engine: abstract base classes under `core/<domain>/`,
  the registry (`registry/`), middleware (`middleware/`), the event bus
  (`event/`), the context/boot (`context.py`), the CLI (`cli.py`), settings, i18n.
- **`horus_builtin`** — the first-party plugins, each registered through a
  `horus.*` entry point in `pyproject.toml`. This is the reference for how any
  plugin is written. Both packages ship in the wheel.

## The layer model

```
Workflow  (BaseWorkflow)   orchestrates a DAG of tasks
  └─ Task (BaseTask)        one unit of work; a DAG node
       ├─ Runtime  (BaseRuntime)   WHAT to run  (command / script / function)
       ├─ Executor (BaseExecutor)  HOW  to run it (shell / exec / in-memory)
       ├─ Target   (BaseTarget)    WHERE it runs (local / ssh / cloud)
       │    └─ Channel  (ChannelProcess, JobHandle)  agentless command + I/O
       └─ Artifacts (BaseArtifact) file-backed inputs/outputs (existence = done)

Edges (WorkflowEdge)     the sole source of truth for the DAG + transfer sources
Transfer (BaseTransferStrategy)  moves an artifact source-target → dest-target
Interaction (BaseInteraction + Transport + Renderer)   user prompts
Events (BaseEvent, HorusEventBus, BaseEventSubscriber)  pub/sub; live logs, TUI
Middleware (AutoMiddleware)   before/after/wrap hooks around each layer
```

Key idea: a **Task** binds a Runtime + Executor + Target + Artifacts. The Executor
runs the Runtime *through the Target's channel*, so one executor works both local
and remote. Artifact existence (and SHA-256 hash) decides whether a task is
already complete and can be skipped.

## Boot flow — `HorusContext.boot()` (`context.py`)

`HorusContext` is a dataclass holding the event `bus`, the current `workflow`, and
a scratch `data` dict. It lives in a `ContextVar`; anywhere in the runtime you can
call `HorusContext.get_context()`.

`boot()` must run once before any registry-typed model is instantiated. It:

1. `AutoRegistry.init_registry()` — scans every entry-point group starting with
   `horus.` and calls `.load()` on each entry (importing the plugin module, which
   triggers `__init_subclass__` and registers the class). A plugin that fails to
   import is logged and skipped, not fatal.
2. `AutoMiddleware.init_registry()` — same, for `horus.middleware.*` groups.
3. `ctx.bus.start()` — instantiates and starts all registered bus transports and
   event subscribers.
4. stores the context in the `ContextVar` and emits `HorusRuntimeReadyEvent`.

`ctx.shutdown()` emits `HorusRuntimeWillShutdownEvent` and stops the bus.

## Run flow — `horus run WF.yaml`

From `cli.py`:

```python
ctx = HorusContext.boot()
workflow = BaseWorkflow.from_yaml(workflow_yaml)   # registry dispatch on `kind`
trigger = trigger_id or workflow.tasks[0].id
render_workflow(workflow, trigger_id=trigger)      # live TUI (or asyncio.run with --no-tui)
ctx.shutdown()
```

`BaseWorkflow.run(trigger_id)` (the `@final` public entry) sets status, enforces
one workflow at a time, sets `ctx.workflow = self`, and wraps the concrete
`_run(trigger_id)` in workflow middleware. `HorusWorkflow._run`:

1. `execution_plan(tasks, trigger_id, edges)` — topological order, scoped to the
   trigger's ancestors ∪ descendants (see `horus_builtin/workflow/dag.py`).
2. `_build_source_map()` once — resolves each `(task, input)` to its producing
   target + artifact from the edges.
3. For each task in order: `task.target.bind(task)` → `transfer_artifacts(task,
   source_map)` → `task.target.dispatch(task)` → `task.target.wait()`.

Per task, `dispatch` sets `PENDING`, runs target middleware, and `_dispatch`
schedules `task.run()` as an asyncio task. `task.run()` (final) skips if
`skip_if_complete and is_complete()`, else drives `RUNNING → COMPLETED/CANCELED/
FAILED` around `_run()`. `HorusTask._run` verifies inputs exist, then calls
`executor.execute(task)`, which renders the runtime and runs it over the channel.

## The channel / detach layer

`core/target/channel.py` provides the agentless command abstraction so executors
never touch a raw subprocess directly:

- `ChannelProcess` — a process handle: `returncode`, `wait()`, `communicate()`,
  `kill()`, `signal()`, and `stream()` which yields `(StreamName, bytes)` lines.
- `merge_line_streams(stdout, stderr)` — merges the two pipes into one async
  line generator via a bounded queue. This feeds the **live logs**: `ShellExecutor`
  iterates `proc.stream()` and logs each line as it arrives, which the TUI renders
  in its log pane.
- `JobHandle`, `PollingChannelProcess`, `build_detach_command`, `new_job_dir` —
  the **detached** execution path.

`BaseTarget.run_command(cmd, *, cwd, env, detach=None)` is a template method:
`detach` defaults to `self.detach_by_default`. When not detached it calls
`run_command_sync` (a live channel). When detached it makes a `.horus_job/<id>`
dir and calls `launch(...)`, returning a `PollingChannelProcess` that reads
`pid`/`exit_code`/`stdout.log`/`stderr.log` from that dir by polling every
`poll_interval` seconds. Detachment lets a launched job **survive the channel that
started it** (e.g. a dropped SSH connection) and is the groundwork for
`recover()`. `LocalTarget` sets `detach_by_default = False` (no droppable channel
locally) and `poll_interval = 0.25`.

A target implements: `run_command_sync`, `launch`, `poll`, `read_output`,
`send_signal`, plus the filesystem ops `put_file`, `get_file`, `mkdir`, `list_dir`
(→ `RemoteDirEntry`). `run_command` itself is provided by the base.

## Event bus & middleware (brief)

- **Events** (`event/`): plugins emit `BaseEvent` subclasses via
  `HorusContext.get_context().bus.emit(...)`. Subscribers (`BaseEventSubscriber`)
  declare which event types they care about and handle them — used for loguru
  logging, timing, and the TUI.
- **Middleware** (`middleware/`): each layer (task, workflow, executor, runtime,
  target, transfer, interaction) has a root that wraps the corresponding `_hook`
  with `before` / `after` / `wrap`. `task` and `workflow` are the two groups
  exposed as external entry points. See `references/registry.md`.
