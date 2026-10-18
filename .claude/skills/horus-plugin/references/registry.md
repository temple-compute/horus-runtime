# Registry internals

The registration and dispatch machinery that makes plugins work. Three pieces:
`AutoRegistry` (most plugins), `AutoRegistryProduct` (composite-keyed plugins),
and `AutoMiddleware` (middleware). Paths are under `src/horus_runtime/`.

## AutoRegistry — `registry/auto_registry.py`

`class AutoRegistry(BaseModel, ABC)`. A Pydantic model that auto-registers its
concrete subclasses in a per-hierarchy registry and dispatches to them by a
discriminator field.

### Declaring a registry root

Pass `entry_point="<name>"` in the class definition. This:

- prefixes the group to `horus.<name>` (`HORUS_ENTRY_POINT_PREFIX = "horus."`),
- gives the class its own fresh `registry: dict[str, type]`,
- records it in `_registry_roots` (used by Pydantic dispatch).

Root classes are **not** themselves registered as concrete implementations.

```python
class BaseArtifact[T](AutoRegistry, entry_point="artifact"):
    registry_key: ClassVar[str] = "kind"   # which field is the discriminator
    kind: str
```

`registry_key` names the field whose value is the registry key — by convention
`"kind"` everywhere except `BaseEventSubscriber` (`"subscriber_type"`),
`BaseEvent` (`"event_type"`) and `BaseBusTransport` (`"transport_type"`).

Registry roots in `src/`: `artifact`, `runtime`, `executor`, `task`,
`workflow`, `target`, `transfer`, `evacuator`, `interaction`,
`interaction_transport`, `interaction_renderer`, `subscriber`, `event`,
`transport`. `BaseEvent` and `BaseInteractionTransport` set
`add_to_registry = False` on the root itself.

### Auto-registration — `__init_subclass__`

When a subclass is defined:

- Parameterized generic aliases (`JSONArtifact[str]`, name contains `[`) are
  skipped — the origin class already registered.
- If `entry_point=` is passed → it becomes a new root (see above).
- Otherwise the class registers into the nearest root's `registry`, keyed by the
  value of the `registry_key` field on the class.
- **Skipped** when the class is abstract (`inspect.isabstract`) or sets
  `add_to_registry = False`. Use the latter for shared intermediate bases you
  don't want instantiated.

Guardrail exceptions (`registry/exceptions.py`):

- `BaseRegistryClassEntryPointNotDefinedError` — a base inheriting `AutoRegistry`
  that neither passes `entry_point=` nor already has a `registry`.
- `RegistryPointExistsError` — two roots claim the same `horus.<name>` group.
- `RegistryKeyAttributeNotDefinedError` — concrete class has no `registry_key`.
- `RegistryKeyIsNoneError` — the `registry_key` field has an empty/None value.
- `DuplicatedRegistryKeyError` — two concrete classes share a `kind` in one domain.

### Discriminator dispatch — `__get_pydantic_core_schema__` (`@final`)

This is why YAML/JSON "just works". When a field is typed as a registry root
(e.g. `runtime: BaseRuntime`), validation intercepts the incoming dict, reads
`data[registry_key]` (the `kind`), looks up `registry[kind]`, and validates the
dict as that concrete subclass. Unknown/missing `kind` raises a `ValueError`
listing the registered keys. The same hook emits an OpenAPI/JSON schema (base
fields + `additionalProperties: true`) for the GUI. **Corollary:** always type
your fields and load through the base class, so dispatch runs.

### Discovery — `init_registry(bases=None)` (`@final @staticmethod`)

Called at `HorusContext.boot()`. Iterates `entry_points().groups` for groups
starting with `horus.` (or only the groups for the given `bases`; the default
scan also imports `horus.middleware.*` modules) and `.load()`s
each entry point, importing the module and triggering registration. A plugin that
raises on import is logged via `horus_logger` and skipped so one broken plugin
can't take down the rest.

## AutoRegistryProduct — `registry/auto_registry_product.py`

For plugins whose key is **composed** from the `kind` defaults of *other*
registry types, rather than a single `kind`. Used by transfer strategies and
interaction renderers.

`registry_key` uses the form `"<field>:<attrA>.<attrB>"`. Example:

```python
class BaseTransferStrategy[S, D](
    AutoRegistryProduct, AutoRegistry, entry_point="transfer"
):
    registry_key: ClassVar[str] = "transfer_key:handles_source.handles_destination"
    transfer_key: str | None = None
    handles_source: ClassVar[type[BaseTarget]]
    handles_destination: ClassVar[type[BaseTarget]]
```

The concrete key is derived from the two targets' `kind` defaults, e.g.
`"local.local"`. Look one up with `get_from_registry(*args)`:

```python
strategy_cls = BaseTransferStrategy.get_from_registry(source_target, dest_target)
```

**MRO matters:** `AutoRegistryProduct` must come **before** `AutoRegistry` in the
base list.

## AutoMiddleware — `middleware/auto_middleware.py`

Separate machinery for middleware. Prefix is
`HORUS_MIDDLEWARE_ENTRY_POINT_PREFIX = "horus.middleware."`. Unlike `AutoRegistry`
the registry is a **list**, not a dict — many middlewares can stack on one domain.

Per-domain roots and the group each loads from (`middleware/<file>.py`):

| Root | Group | Context fields | Wraps |
| --- | --- | --- | --- |
| `TaskMiddleware` | `horus.middleware.task` | `task` | `BaseTask._run` (and the working-dir mkdir) |
| `WorkflowMiddleware` | `horus.middleware.workflow` | `workflow` | `BaseWorkflow._run` |
| `ExecutorMiddleware` | `horus.middleware.executor` | `executor`, `task` | `BaseExecutor._execute` |
| `RuntimeMiddleware` | `horus.middleware.runtime` | `runtime`, `task` | `BaseRuntime._setup_runtime` |
| `TargetMiddleware` | `horus.middleware.target` | `target`, `task` | `BaseTarget._dispatch` |
| `BoundTaskMiddleware` | `horus.middleware.bound_task` | `task`, `target`, `input_digests` (lazy, shared with the check) | one attempt of a task on its bound target, from the pre-transfer completion check to the end of the run (scheduler `_execute_on_target`); runs for skipped tasks too, and again per node-loss retry; `after` must not raise |
| `TargetCommandMiddleware` | `horus.middleware.target_command` | `target`, `command` (mutable), `cwd`, `env`, `detach` | `BaseTarget._run_command` |
| `TransferMiddleware` | `horus.middleware.transfer` | `transfer_strategy`, `artifact`, `source`, `destination` | `BaseTransferStrategy._transfer` |
| `InteractionMiddleware` | `horus.middleware.interaction` | `transport`, `interaction`, `renderer`, `attempt` | one render attempt in `ask` |

`AutoMiddleware.init_registry()` loads every `horus.middleware.*` group at boot,
so all nine are usable from a plugin.

A middleware overrides any of these, all `async`:

```python
async def before(self, context: Ctx) -> None: ...
async def after(self, context: Ctx) -> None: ...
async def wrap(self, context: Ctx, call_next: Callable[[], Awaitable[R]]) -> R: ...
```

The default `wrap` awaits `before`, then `call_next()`, then `after` in a
`finally`. The layer runs its `_hook` inside
`<Domain>Middleware.call_with_middleware(<Domain>MiddlewareContext(...), call)`;
each middleware class is instantiated per call, in registry order.

Reference middleware: `horus_builtin/middleware/task_time.py` (overrides `wrap`,
times the task and emits a `HorusTaskEvent` with how it ended).
