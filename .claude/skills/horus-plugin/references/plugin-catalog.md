# Plugin catalog

Per-domain contract: base class, the fields/`ClassVar`s to set, the abstract
methods to implement, and the built-in to copy. Base paths are under
`src/horus_runtime/`; built-in refs under `src/horus_builtin/`.

Common to every domain: set `kind: str = "<your_kind>"` (default value required),
plus `kind_name` / `kind_description` `ClassVar`s. Implement only the abstract
`_`-prefixed hooks — never override the `@final` public method.

---

## Artifact — `horus.artifact`

`BaseArtifact[T]` (`core/artifact/base.py`), `registry_key="kind"`. Generic over
the native value type. File-backed: the artifact describes data, it does not
touch the target's filesystem itself.

Fields: `id`, `name` (defaults to `id`), `description`, `labels: dict[str, str]`
(free metadata, never part of a cache key), `path: Path` (resolved absolute;
`declared_path` keeps the original). Implement:

```python
@abstractmethod
def read(self) -> T: ...
@abstractmethod
def write(self, value: T) -> None: ...
```

Overridable:

- `materialize()` — make the bytes exist at `path`; no-op by default.
- `pack_command(src, pkg) -> str | None` — POSIX command that packs the
  artifact at `src` into the single file `pkg`; `None` (default) means the
  artifact is already one file.
- `unpack_command(pkg, dest) -> str | None` — inverse; `None` means the store
  just moves `pkg` to `dest`.

Existence, digest, delete, package and unpackage live in `ArtifactStore`
(`core/artifact/store.py`), bound to a target and run through its primitives:
`ArtifactStore(target).exists(a)`, `.digest(a)` (sha256, folders hash a sorted
file listing), `.delete(a)`, `.package(a) -> str`, `.unpackage(a, pkg)`. The
store calls `artifact.emit_event(ArtifactEventsEnum.…)` after `delete`, `package`
and `unpackage`.
**Ref:** `artifact/folder.py` (`FolderArtifact(BaseArtifact[Path])`,
`kind="folder"`, tar `pack_command`/`unpack_command`).

Value kinds (`number`, `boolean`, `string`, `choice`, `multichoice`, `list`) build on
`ValueArtifact[T]` (`horus_builtin/artifact/value.py`): a `value: T` field and
a `materialize()` that writes `value` to `path` only when no file exists yet.
Subclass it and implement `read`/`write`.

Iterable collections additionally mix in `IterableArtifact`
(`core/artifact/iterable.py`; plain ABC, not a registry root, not a kind).
Implement:

```python
@abstractmethod
async def items(self, target: "BaseTarget") -> list[BaseArtifact]: ...
```

Return a deterministic list of real artifacts, each id'd `f"{self.id}:{slot}"`
and materialized on *target* so a consumer can hand one straight to a task;
raise `ArtifactIterationError` when enumeration is impossible. **Refs:**
`artifact/folder.py` (children sorted by name, pointing at their own
on-target paths, zero copy), `artifact/json.py` (parsed list; one
single-element JSON artifact per zero-padded index, in a `<stem>.items`
directory next to the parent).

## Runtime — `horus.runtime`

`BaseRuntime[T]` (`core/runtime/base.py`), `registry_key="kind"`. Describes *what*
to run; returns the prepared value the executor consumes.

```python
@abstractmethod
async def _setup_runtime(self, task: "BaseTask") -> T: ...
```

Public `setup_runtime()` is `@final` (wraps runtime middleware). **Ref:**
`runtime/command.py` (`CommandRuntime(BaseRuntime[str])`, `kind="command"`, does
`$`/`${}` substitution via `substitution.substitute`, emits `RuntimeEvent`,
returns the rendered command string).

## Executor — `horus.executor`

`BaseExecutor` (`core/executor/base.py`), `registry_key="kind"`. Runs a task's
runtime, usually over `task.target`'s channel.

```python
runtimes: ClassVar[RuntimeFilterType] = (CommandRuntime,)   # accepted runtimes
@abstractmethod
async def _execute(self, task: "BaseTask") -> None: ...
```

`execute()` is `@final`: it makes the side-artifacts dir and each output's
parent dir on the target, sets the task `RUNNING`, runs `_execute` inside
executor middleware, and always calls `collect_side_artifacts(task)` afterward
(pulls `task.side_artifacts_dir` back over the channel). An executor with slow
setup of its own (image pull, queueing) may set `CONFIGURING` at the start of
`_execute` and `RUNNING` once real work starts. Override `cancel_execution()`
to kill an external process on cancel. The `runtimes` tuple is
validated against the task's runtime at task construction
(`IncompatibleRuntimeError`). **Ref:** `executor/shell.py` (`ShellExecutor`,
`kind="shell"`, `runtimes=(CommandRuntime,)`; runs `task.target.run_command(...)`
and streams stdout/stderr live).

## Task — `horus.task`

`BaseTask` (`core/task/base.py`), `registry_key="kind"`. A DAG node binding a
runtime + executor + target + artifacts.

Fields: `id` (DAG key, may not start with `@`), `definition_id` (catalog
source, not unique), `name`, `description`, `inputs`/`outputs`/`side_artifacts`
(`list[BaseArtifact]`), `unfingerprinted_inputs: list[str]` (input ids left out
of the fingerprint; a map sets it on clones), `executor`, `runtime`, `target`
(required on the base; `HorusTask` defaults to `LocalTarget`; a document may
say `{ref: name}`), `resources: ResourceRequest | None`, `status`, `skip_reason`
(`complete`/`inactive`/`upstream_failed`, only with `SKIPPED`), `error`
(`"<Type>: <message>"`, only with `FAILED`), `started_at`/`finished_at` (UTC),
`runs`, `skip_if_complete` (default `True`), `interaction`. ClassVar
`dispatches_children` (default `False`; `True` for a map, so it takes no
`member_cap` slot or placement). Implement:

```python
@abstractmethod
async def _run(self) -> None: ...           # do NOT set self.status here
@abstractmethod
async def is_complete(self) -> bool: ...
@abstractmethod
async def _reset(self) -> None: ...
```

Optional: `async is_complete_before_transfer(input_digests) -> bool` — the
scheduler's skip check before inputs are staged; *input_digests* is an async
callable giving each input's digest at its source. Default `False` (transfer,
then `run()` checks `is_complete`).

Optional: `async manifest_matches(manifest: bytes, input_digests) -> bool` —
whether a completion manifest held elsewhere (e.g. a cache) matches the
task's current inputs and config, without reading the target. Default
`False`.

`run()` / `reset()` are `@final` and drive status + middleware; `reset()`
clears `status`, `skip_reason`, `error` and the timestamps before `_reset()`.
**Ref:** `task/horus_task.py` (`HorusTask`, `kind="horus_task"`: `_run` checks
inputs exist via `ArtifactStore`, fingerprints them, awaits
`executor.execute(self)`, then writes the manifest
`<working dir>/.horus/<id>.json`; `is_complete` → every output exists and the
manifest matches the input digests and config, `False` when there are no
outputs; `is_complete_before_transfer` → `is_complete(input_digests)`;
`manifest_matches` → the bytes parse as its manifest and equal the fingerprint
from *input_digests*; `_reset`
deletes outputs and the manifest).

## Workflow — `horus.workflow`

`BaseWorkflow` (`core/workflow/base.py`), `registry_key="kind"`. Orchestrates the
DAG.

Fields: `id: UUID`, `name` (any text without control characters),
`workflow_slug` (filesystem-safe id set by the host), `run_scope` (path
fragment under non-co-located targets' working dirs, set by the host before
`run`), `targets: dict[str, BaseTarget]` (named shared targets), `tasks`,
`artifacts` (root artifacts), `edges: list[WorkflowEdge]` (the DAG source of
truth), `orchestrator_target`, `status`, `max_concurrency: int | None` (`>= 0`;
unset = scheduler default 8, `0` = no limit), `capacity: dict[location_id,
ResourceCapacity] | None` (gates tasks that declare `resources`),
`failure_policy: "fail_fast" | "continue"`. Implement:

```python
@abstractmethod
async def _run(self, trigger_id: str) -> None: ...
@abstractmethod
async def _reset(self) -> None: ...
```

`reset()` is `@final`; `run(trigger_id)` is the undecorated public driver (do
not override). `from_yaml`/`to_yaml` are classmethods. Model validators enforce
unique task ids, per-task unique artifact ids, that every edge resolves, and
shared-target rules (`SharedTargetError`). `transfer_artifacts(task,
source_map)` moves inputs via the registered strategy or the source's
`fallback_transfer()`. `workflow.leases` / `workflow.placement` hold the run's
`LeaseManager` and `PlacementManager`. **Ref:** `workflow/horus_workflow.py`
(`HorusWorkflow`, `kind="horus_workflow"`, `_run` awaits
`scheduler.run_schedule`). See the `horus-workflow` skill for authoring
workflows. Composite-task **Ref:** `workflow/map.py` (`MapTask`,
`kind="horus_map"`, `dispatches_children = True`: clones itself as a plain
`horus_task` once per item of an iterable input (`over: {input_id, as}`), each
clone writing into its own slot of a single folder output. Clones run through
`execute_task` with their own `TargetPool(max_concurrency)` and get
`target.idle_copy()`, so they share the map's machine. `on_item_failure:
"fail" | "drop"`: `drop` removes a failed clone's slot and completes the map
unless every clone failed).

## Target — `horus.target`

`BaseTarget` (`core/target/base.py`), `registry_key="kind"`. Describes *where* a
task runs and provides the channel primitives. The largest interface.

Field: `working_directory` (defaults to cwd). Abstract members:

```python
@property
@abstractmethod
def location_id(self) -> str: ...           # e.g. "local://host", "ssh://user@box"
@abstractmethod
def access_cost(self, artifact) -> float | None:  # 0.0 local, >0 remote, None inaccessible
@abstractmethod
async def run_command_sync(self, cmd, *, cwd=None, env=None) -> ChannelProcess: ...
@abstractmethod
async def launch(self, cmd, *, cwd, env, job_dir) -> JobHandle: ...
@abstractmethod
async def poll(self, handle) -> int | None: ...       # None running, exit code done
@abstractmethod
async def read_output(self, handle) -> tuple[bytes, bytes]: ...
@abstractmethod
async def send_signal(self, handle, sig) -> None: ...
@abstractmethod
async def put_file(self, content: bytes | Path, remote_path: str) -> None: ...
@abstractmethod
async def get_file(self, remote_path: str) -> bytes: ...
@abstractmethod
async def mkdir(self, path: str) -> None: ...
@abstractmethod
async def list_dir(self, path: str) -> list[RemoteDirEntry]: ...
```

`ClassVar`s: `poll_interval` (default `1.0`), `detach_by_default` (default `True`).
Provided by the base: `run_command` (template method over `run_command_sync` /
`launch`, routed through `TargetCommandMiddleware`), `bind`, `dispatch`
(`@final`), `_dispatch`, `wait`, `cancel`, `get_status`, `recover` (default
`False`), `path_on_target`, `resource_scope`, `idle_copy`, `is_colocated_with`,
`resolved_working_directory` (raises when `working_directory` is unset).
Overridable defaults built on the shell channel: `get_file_to`, `read_range`,
`path_exists`, `remove`, and `fallback_transfer()` (→ `GenericTransfer`).

Lease hooks (for a machine the target can destroy; see "Releasable targets and
leases" in `references/architecture.md`): `releasable` ClassVar,
`hold_timeout_s`, `member_cap`, `is_live`, `generation`, `node_capacity()`,
`validate_member(task)`, `validate_named()`, `on_ready(cb)`,
`async release(reason)`, `async keepalive()`, `emit_node_phase(phase)`.
**Ref:** `target/local.py` (`LocalTarget`, `kind="local"`,
`location_id="local://<hostname>"`, all primitives via
`asyncio.create_subprocess_shell` with process-group isolation;
`detach_by_default=False`).

## Transfer strategy — `horus.transfer`

`BaseTransferStrategy[S, D]` (`core/transfer/strategy.py`). **Product registry** —
inherit `AutoRegistryProduct, AutoRegistry` (in that order), no `kind`.

```python
handles_source: ClassVar[type[BaseTarget]] = LocalTarget
handles_destination: ClassVar[type[BaseTarget]] = LocalTarget
@abstractmethod
async def _transfer(self, artifact, source: S, destination: D) -> None: ...
```

Key is derived as `"<source_kind>.<dest_kind>"`. `transfer(artifact, source,
dest, *, skip_if_present=False)` is `@final`: equal `location_id` only repoints
`artifact.path`, otherwise it runs transfer middleware around `_transfer`. Move
bytes with `source.get_file_to(...)` / `destination.put_file(...)`, or
`ArtifactStore(source).package(a)` / `ArtifactStore(dest).unpackage(a, pkg)`.
A pair with no registered strategy uses `source.fallback_transfer()`, by
default `GenericTransfer` (`core/transfer/generic.py`, `add_to_registry =
False`): package on the source, stream through orchestrator disk, unpackage on
the destination. **Ref:** `transfer/local_noop.py` (`LocalNoOpTransfer`,
`handles_source = handles_destination = LocalTarget`, no-op).

## Evacuator — `horus.evacuator`

`BaseEvacuator` (`core/lease.py`), `registry_key="kind"`. Moves an output off a
releasable machine before it is destroyed.

```python
priority: ClassVar[int] = 0                 # highest registered one is used
@abstractmethod
async def evacuate(self, task, output, source: BaseTarget) -> EdgeSource: ...
```

Return where consumers read the output from afterwards; never touch *source*
after returning. **Ref:** `OrchestratorEvacuator` (`kind="orchestrator"`,
copies to `<orchestrator wd>/.evacuated/<task_id>/`).

## Interaction trio — `horus.interaction*`

For user prompts. Three cooperating plugins:

- `BaseInteraction[T]` (`core/interaction/base.py`, `horus.interaction`):
  abstract `async parse(self, value) -> T`. Ref `interaction/common/string.py`
  (`StringInteraction`, `kind="string"`).
- `BaseInteractionTransport` (`core/interaction/transport.py`,
  `horus.interaction_transport`): the delivery channel; `ask()` is `@final`
  (render + parse + retry). Ref `interaction/cli.py` (`CLIInteractionTransport`,
  `kind="cli"`).
- `BaseInteractionRenderer[T, I]` (`core/interaction/renderer.py`,
  `horus.interaction_renderer`): **product registry** keyed on
  `handles_transport.handles_interaction`; abstract
  `async render(self, transport, interaction)`. Ref `interaction/cli.py`
  (`CLIStringRenderer`, etc.).

## Event subscriber — `horus.subscriber`

`BaseEventSubscriber[E]` (`event/subscriber.py`), `registry_key="subscriber_type"`.

```python
subscriber_type: str = "loguru"
events: ClassVar[tuple[type[BaseEvent], ...]] = (BaseEvent,)   # which events to receive
@abstractmethod
def setup(self) -> None: ...
@abstractmethod
def handle(self, event: E) -> None: ...
```

**Ref:** `event/log_subscriber.py` (`LogsSubscriber`, `subscriber_type="loguru"`).
Note the discriminator here is `subscriber_type`, not `kind`.

## Middleware — `horus.middleware.<domain>`

Subclass the domain root: `TaskMiddleware`, `WorkflowMiddleware`,
`ExecutorMiddleware`, `RuntimeMiddleware`, `TargetMiddleware`,
`TargetCommandMiddleware`, `TransferMiddleware` or `InteractionMiddleware`
(`middleware/<domain>.py`), and register under `horus.middleware.<domain>`.
Override any of (all async):

```python
async def before(self, ctx: TaskMiddlewareContext) -> None: ...
async def after(self, ctx: TaskMiddlewareContext) -> None: ...
async def wrap(self, ctx, call_next): ...    # full control around the call
```

The context dataclass exposes the layer objects (`ctx.task`, `ctx.workflow`,
`ctx.target`, ...; field list in `references/registry.md`).
`TargetCommandMiddlewareContext.command` is mutable, so a middleware can wrap
the command before it runs. **Ref:** `middleware/task_time.py`
(`TaskTimeMiddleware` overrides `wrap`, times the task and emits
`HorusTaskEvent`).
