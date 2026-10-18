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
       └─ Artifacts (BaseArtifact) file-backed inputs/outputs

Edges (WorkflowEdge)     the sole source of truth for the DAG + transfer sources
Transfer (BaseTransferStrategy)  moves an artifact source-target → dest-target
ArtifactStore (core/artifact/store.py)  exists/digest/delete/package/unpackage
LeaseManager + BaseEvacuator (core/lease.py)  releasable machines (cloud nodes)
Interaction (BaseInteraction + Transport + Renderer)   user prompts
Events (BaseEvent, HorusEventBus, BaseEventSubscriber)  pub/sub; live logs, TUI
Middleware (AutoMiddleware)   before/after/wrap hooks around each layer
```

Key idea: a **Task** binds a Runtime + Executor + Target + Artifacts. The Executor
runs the Runtime *through the Target's channel*, so one executor works both local
and remote. A `HorusTask` is complete (and skipped) when every output exists on
its target and the manifest `.horus/<task_id>.json` still matches its input
digests and config.

## Boot flow — `HorusContext.boot()` (`context.py`)

`HorusContext` is a dataclass holding the event `bus`, the current `workflow`, and
a scratch `data` dict. It lives in a `ContextVar`; anywhere in the runtime you can
call `HorusContext.get_context()`.

`boot()` must run once before any registry-typed model is instantiated. It:

1. `AutoRegistry.init_registry()` — scans every entry-point group starting with
   `horus.` (this includes `horus.middleware.*`) and calls `.load()` on each entry (importing the plugin module, which
   triggers `__init_subclass__` and registers the class). A plugin that fails to
   import is logged and skipped, not fatal.
2. `AutoMiddleware.init_registry()` — same, for `horus.middleware.*` groups.
3. `ctx.bus.start()` — instantiates and starts every registered
   `BaseBusTransport`, then instantiates each `BaseEventSubscriber`, calls
   `setup()` and subscribes it.
4. stores the context in the `ContextVar` and emits `HorusRuntimeReadyEvent`.

`ctx.shutdown()` emits `HorusRuntimeWillShutdownEvent` and stops the bus.

## Run flow — `horus run WF.yaml`

From `cli.py`:

```python
ctx = HorusContext.boot()
workflow = BaseWorkflow.from_yaml(workflow_yaml)   # registry dispatch on `kind`
trigger = trigger_id or workflow.tasks[0].id
# --no-skip ID / --no-skip-all set task.skip_if_complete = False
render_workflow(workflow, trigger_id=trigger)      # live TUI (or asyncio.run with --no-tui)
ctx.shutdown()
```

`BaseWorkflow.run(trigger_id)` sets status, enforces one workflow at a time,
sets `ctx.workflow = self`, anchors paths to the run directory, and wraps
`_run(trigger_id)` in workflow middleware. `HorusWorkflow._run` calls
`run_schedule` (`horus_builtin/workflow/scheduler.py`), a concurrent ready-set
scheduler, not a serial loop:

1. Each iteration recomputes the scope with `trigger_scope` (`dag.py`): the
   trigger, its descendants, and the ancestors of every descendant. Tasks added
   mid-run (`add_task`/`expand`, e.g. map clones, subworkflows) are picked up.
2. Every task in scope whose dependencies are all completed is dispatched at
   once, sorted by id. A branch gate runs first: a task with no live incoming
   edge is `SKIPPED` (`skip_reason=inactive`).
3. `asyncio.wait(FIRST_COMPLETED)`; finished tasks join `completed` and their
   dependents become ready. `workflow.leases.reconcile(...)` runs every
   iteration.
4. On failure: `failure_policy="fail_fast"` cancels everything in flight and
   re-raises; `"continue"` blocks the failed task's descendants (`SKIPPED`,
   `skip_reason=upstream_failed`), lets other branches finish, then raises
   `WorkflowExecutionError`. Nothing ready and nothing running with tasks left
   raises `CyclicDependencyError` or `StalledWorkflowError`.
5. At the end (or on any error) `leases.aclose(...)` releases every live
   releasable machine.

Per task, `execute_task` (shared with `horus_map`, which dispatches its own
clones) does:

1. `leases.member_slot(task)` — waits for a `member_cap` slot.
2. `placement.acquire(...)` — waits until `task.resources` fit the location's
   declared `capacity` (no-op without `capacity` or `resources`).
3. `pool.acquire(target)` — `TargetPool` bounded by `max_concurrency` (unset =
   8, `0` = no limit); a target instance already in use is handed out again as
   `idle_copy()`.
4. `target.bind(task)`, `leases.member_started(task)`.
5. Skip check before transfer: if `skip_if_complete` and
   `await task.is_complete_before_transfer(...)` (judged from source digests),
   mark `SKIPPED` (`skip_reason=complete`) and return.
6. `workflow.transfer_artifacts(task, source_map)` — per input, the registered
   strategy for `(source, dest)` or `source.fallback_transfer()`.
7. `target.dispatch(task)` → `target.wait()`.

`dispatch` (final) sets `PENDING`, runs target middleware, and `_dispatch`
schedules `task.run()` as an asyncio task. `task.run()` (final) checks
`skip_if_complete and await is_complete()` again, else sets `CONFIGURING`,
mkdirs `working_dir`, and runs `_run()` inside task middleware;
`BaseExecutor.execute` flips the status to `RUNNING`. It ends `COMPLETED`,
`CANCELED` or `FAILED` (with `error`, `started_at`, `finished_at` set).
`HorusTask._run` verifies inputs exist, fingerprints them, calls
`executor.execute(task)`, and writes the manifest on success.

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

`run_command` first runs `TargetCommandMiddleware`, which may rewrite the
command, then the detach logic above (`_run_command`).

A target implements: `run_command_sync`, `launch`, `poll`, `read_output`,
`send_signal`, plus the filesystem ops `put_file`, `get_file`, `mkdir`, `list_dir`
(→ `RemoteDirEntry`). `run_command` itself is provided by the base.

Base defaults worth overriding on a remote target:

- `get_file_to(remote, local)` — reads the whole file via `get_file`; override
  to stream to disk.
- `read_range(remote, offset, length)` — async iterator, `dd | head -c` over
  `run_command_sync`; `offset` must be MiB-aligned.
- `path_exists(path)` — `test -e` over the shell; `remove(path)` — `rm -rf`.
- `fallback_transfer()` — `GenericTransfer()`: package on the source,
  `get_file_to` to orchestrator disk, `put_file`, unpackage on the destination.
- `idle_copy()` — `model_copy()` with the bound task cleared; another slot on
  the same machine (shares `lease_key`). A `deepcopy` gets a new `lease_key`.
- `async resource_scope(task, process)` — `None`; override when the target reshapes
  execution (a scheduler queue).
- `recover()` — `False`.

## Releasable targets and leases

A target that owns a machine it can destroy (e.g. a terraform node) sets
`releasable` true (a ClassVar, or a property: terraform returns
`auto_teardown`) and overrides the lease hooks on
`BaseTarget` (`core/target/base.py`). The defaults make a target non-releasable.

| Hook | Default | Meaning |
| --- | --- | --- |
| `hold_timeout_s` (property) | `None` | Seconds the machine may idle while members are still pending before it is drained and released. |
| `member_cap` (property) | `None` | Max members running at once on the machine (not named `max_parallel` so a subclass field of that name is not shadowed). |
| `is_live` (property) | `False` | Whether the machine exists right now. |
| `generation` (property) | `0` | Incremented on each successful provision. |
| `node_capacity()` | `None` | Machine size; declared to `PlacementManager` unless the workflow's `capacity` already names the location. |
| `validate_member(task)` | `[]` | Errors for *task* using this named target. |
| `validate_named()` | `[]` | Errors for this target declared under `targets:`. |
| `on_ready(callback)` | no-op | Register a coroutine to run once per generation after provisioning, before any member dispatches. Nothing in horus-runtime calls it; hosts do. |
| `release(reason)` | no-op | Destroy the machine. Idempotent, safe during provisioning, raises on failure. `reason`: `done`, `drained`, `idle_cap`, `run_end`, `abort`. |
| `keepalive()` | no-op | Called every 300 s (30 s timeout) while busy or holding, so idle watchdogs and lifetime caps do not fire. Must not raise. |
| `emit_node_phase(phase, **fields)` | no-op | Publish `busy`, `holding`, `draining`, `drain_failed`, ... |

Named targets (`workflow.targets`, task `target: {ref: name}`) resolve to one
instance shared by every member; the workflow's `_check_shared_targets`
validator collects `validate_named`/`validate_member` errors into
`SharedTargetError`. Leases are keyed by `lease_key` (a uid shared by
`idle_copy` and map clones).

`LeaseManager` (`core/lease.py`, one per run, `workflow.leases`) phases:
`cold` → `busy` (a member running) → `holding` (members pending, none running,
`hold_timeout_s` timer armed) → `draining` (evacuating outputs pending tasks
still need) → `releasing` → `cold`. `reconcile` runs after each scheduler batch.
Hold expiry releases with `idle_cap`; no pending members releases with `done`
(or `drained` after evacuation). A failed drain keeps the machine and lets
consumers pull from it; a failed destroy retries with backoff (60 s, doubling,
max 1 h). `aclose` at run end releases every live machine without evacuation.

Evacuators (`horus.evacuator`): subclass `BaseEvacuator`, set `kind` and
`priority: ClassVar[int]`, implement
`async evacuate(task, output, source) -> EdgeSource`. The registered one with
the highest `priority` is used. Built-in `OrchestratorEvacuator`
(`priority=0`) copies into `<orchestrator wd>/.evacuated/<task_id>/`.


## Event bus & middleware (brief)

- **Events** (`event/`): plugins emit `BaseEvent` subclasses via
  `HorusContext.get_context().bus.emit(...)`. Subscribers (`BaseEventSubscriber`)
  declare which event types they care about and handle them — used for loguru
  logging, timing, and the TUI.
- **Middleware** (`middleware/`): each layer (task, workflow, executor, runtime,
  target, target_command, bound_task, transfer, interaction) has a root that
  wraps the corresponding `_hook` with async `before` / `after` / `wrap`. Every
  `horus.middleware.<domain>` group is loaded at boot. See
  `references/registry.md`.
