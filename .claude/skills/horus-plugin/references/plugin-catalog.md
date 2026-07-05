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
the native value type. File-backed; existence + SHA-256 `hash` drive completion.

Fields: `id`, `name` (defaults to `id`), `description`, `path: Path` (resolved
absolute). Implement:

```python
@abstractmethod
def read(self) -> T: ...
@abstractmethod
def write(self, value: T) -> None: ...
```

Overridable: `exists()`, `hash` (property → `str | None`), `delete()`,
`package() -> Path`, `unpackage(path)`. Emit events with
`self._emit_event(ArtifactEventsEnum.READ)`. **Ref:** `artifact/folder.py`
(`FolderArtifact(BaseArtifact[Path])`, `kind="folder"`, overrides
`hash`/`package`/`unpackage`).

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

`execute()` is `@final`: it makes the side-artifacts dir, runs `_execute` inside
executor middleware, and always calls `collect_side_artifacts(task)` afterward
(pulls `task.side_artifacts_dir` back over the channel). The `runtimes` tuple is
validated against the task's runtime at task construction
(`IncompatibleRuntimeError`). **Ref:** `executor/shell.py` (`ShellExecutor`,
`kind="shell"`, `runtimes=(CommandRuntime,)`; runs `task.target.run_command(...)`
and streams stdout/stderr live).

## Task — `horus.task`

`BaseTask` (`core/task/base.py`), `registry_key="kind"`. A DAG node binding a
runtime + executor + target + artifacts.

Fields: `id` (DAG key), `name`, `inputs`/`outputs`/`side_artifacts`
(`list[BaseArtifact]`), `executor`, `runtime`, `target` (defaults to
`LocalTarget`), `resources: ResourceRequest | None`, `status`, `runs`,
`skip_if_complete` (default `True`), `interaction`. Implement:

```python
@abstractmethod
async def _run(self) -> None: ...           # do NOT set self.status here
@abstractmethod
def is_complete(self) -> bool: ...          # usually: all outputs exist
@abstractmethod
def _reset(self) -> None: ...
```

`run()` / `reset()` are `@final` and drive status + middleware. **Ref:**
`task/horus_task.py` (`HorusTask`, `kind="horus_task"`: `_run` checks inputs exist
then `await self.executor.execute(self)`; `is_complete` → all outputs exist, and
`False` when there are no outputs so it always runs).

## Workflow — `horus.workflow`

`BaseWorkflow` (`core/workflow/base.py`), `registry_key="kind"`. Orchestrates the
DAG.

Fields: `id: UUID`, `name` (pattern `^[a-zA-Z0-9 _-]+$`), `tasks`, `artifacts`
(root artifacts), `edges: list[WorkflowEdge]` (the DAG source of truth),
`orchestrator_target`, `status`. Implement:

```python
@abstractmethod
async def _run(self, trigger_id: str) -> None: ...
@abstractmethod
def _reset(self) -> None: ...
```

`run(trigger_id)` / `reset()` are `@final`. `from_yaml`/`to_yaml` are classmethods.
Three model validators enforce unique task ids, per-task unique artifact ids, and
that every edge resolves. `transfer_artifacts(task, source_map)` moves inputs via
the matching transfer strategy. **Ref:** `workflow/horus_workflow.py`
(`HorusWorkflow`, `kind="horus_workflow"`, DAG execution via
`dag.execution_plan`). See the `horus-workflow` skill for authoring workflows.

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
`launch`, see `references/architecture.md`), `bind`, `dispatch` (`@final`),
`_dispatch`, `wait`, `cancel`, `get_status`, `recover` (default `False`),
`path_on_target`. **Ref:** `target/local.py` (`LocalTarget`, `kind="local"`,
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

Key is derived as `"<source_kind>.<dest_kind>"`. `transfer()` is `@final`. Move
bytes with `source.get_file(...)` / `destination.put_file(...)`, or use
`artifact.package()` / `unpackage()`. **Ref:** `transfer/local_noop.py`
(`LocalNoOpTransfer`, `handles_source = handles_destination = LocalTarget`, no-op).

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

## Middleware — `horus.middleware.task` / `horus.middleware.workflow`

Subclass `TaskMiddleware` (`middleware/task.py`) or `WorkflowMiddleware`
(`middleware/workflow.py`). Override any of:

```python
def before(self, ctx: TaskMiddlewareContext) -> None: ...
def after(self, ctx: TaskMiddlewareContext) -> None: ...
async def wrap(self, ctx, call): ...     # full control around the call
```

The context dataclass exposes the layer object (`ctx.task`, `ctx.workflow`).
**Ref:** `middleware/task_time.py` (`TaskTimeMiddleware` times the task in
`before`/`after` and emits `HorusTaskEvent`). See `references/registry.md` for the
list-based middleware registry.
