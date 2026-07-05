---
name: horus-plugin
description: >-
  Build, register, and troubleshoot horus-runtime plugins — custom targets,
  runtimes, executors, artifacts, tasks, workflows, transfer strategies,
  interactions, event subscribers, and middleware. Use whenever the task
  involves subclassing a Horus base class (BaseTarget, BaseRuntime,
  BaseExecutor, BaseArtifact, BaseTask, BaseWorkflow, BaseTransferStrategy,
  BaseInteraction, BaseEventSubscriber, AutoMiddleware), adding a new "kind" to
  the runtime, registering a plugin via a `horus.*` entry point, extending what
  the runtime can execute or where it can run, or understanding how AutoRegistry
  discovery/dispatch works. Triggers: "horus plugin", "custom target/runtime/
  executor/artifact", "register a plugin", "new kind", "entry point",
  "AutoRegistry", "SSH/remote/Slurm target".
---

# Building horus-runtime plugins

`horus-runtime` is a headless, plugin-first workflow engine. **Every concrete
capability is a plugin** — even the first-party built-ins in `horus_builtin` are
registered exactly the way a third-party plugin is. To extend the runtime you
subclass a base class, set a discriminator, implement the abstract method(s), and
register the module under a `horus.<domain>` entry point. That's the whole model.

This skill covers how to write and register any plugin. To **author workflows**
that use these plugins (YAML or Python), use the companion `horus-workflow` skill.

## The universal plugin contract

Every plugin follows the same four steps:

1. **Subclass the relevant base class** (e.g. `BaseTarget`, `BaseRuntime`). The
   base declares a registry root via `entry_point="<domain>"`.
2. **Set the discriminator field** — almost always `kind: str = "my_kind"`. This
   value becomes the YAML/JSON discriminator and the registry key. Also set the
   `kind_name` and `kind_description` `ClassVar`s (they feed the Horus GUI).
3. **Implement the abstract method(s)** for that domain (see
   `references/plugin-catalog.md`).
4. **Register the *module*** (not the class) under the matching entry-point group
   in your package's `pyproject.toml`. Importing the module is what triggers
   registration (`AutoRegistry.__init_subclass__` runs at class-definition time).

```python
# my_pkg/targets/ssh.py
from typing import ClassVar
from horus_runtime.core.target.base import BaseTarget

class SSHTarget(BaseTarget):
    kind: str = "ssh"                        # <- discriminator + registry key
    kind_name: ClassVar[str] = "SSH"
    kind_description: ClassVar[str] = "Run over SSH."
    # ... implement the abstract members (see references/plugin-catalog.md)
```

```toml
# pyproject.toml of the plugin package
[project.entry-points."horus.target"]
ssh = "my_pkg.targets.ssh"                   # module path, NOT the class
```

At the next `HorusContext.boot()` the runtime scans all `horus.*` entry-point
groups, imports each module, and your subclass self-registers. A YAML task that
says `target: {kind: ssh, ...}` (or Python `SSHTarget(...)`) now resolves to your
class. A plugin package depends only on `horus-runtime`; it does **not** need
`horus_builtin`.

## Plugin domains

Thirteen entry-point groups back the plugin system. Each maps to a base class and
a canonical built-in you can copy. Full signatures live in
`references/plugin-catalog.md`.

| Entry-point group            | Base class (`src/horus_runtime/...`)        | Copy this built-in                         |
| ---------------------------- | ------------------------------------------- | ------------------------------------------ |
| `horus.artifact`             | `core/artifact/base.py` `BaseArtifact[T]`   | `horus_builtin/artifact/folder.py`         |
| `horus.runtime`              | `core/runtime/base.py` `BaseRuntime[T]`     | `horus_builtin/runtime/command.py`         |
| `horus.executor`             | `core/executor/base.py` `BaseExecutor`      | `horus_builtin/executor/shell.py`          |
| `horus.task`                 | `core/task/base.py` `BaseTask`              | `horus_builtin/task/horus_task.py`         |
| `horus.workflow`             | `core/workflow/base.py` `BaseWorkflow`      | `horus_builtin/workflow/horus_workflow.py` |
| `horus.target`               | `core/target/base.py` `BaseTarget`          | `horus_builtin/target/local.py`            |
| `horus.transfer`             | `core/transfer/strategy.py` `BaseTransferStrategy[S,D]` | `horus_builtin/transfer/local_noop.py` |
| `horus.interaction`          | `core/interaction/base.py` `BaseInteraction[T]` | `horus_builtin/interaction/common/string.py` |
| `horus.interaction_transport`| `core/interaction/transport.py` `BaseInteractionTransport` | `horus_builtin/interaction/cli.py` |
| `horus.interaction_renderer` | `core/interaction/renderer.py` `BaseInteractionRenderer[T,I]` | `horus_builtin/interaction/cli.py` |
| `horus.subscriber`           | `event/subscriber.py` `BaseEventSubscriber[E]` | `horus_builtin/event/log_subscriber.py` |
| `horus.middleware.task`      | `middleware/task.py` `TaskMiddleware`       | `horus_builtin/middleware/task_time.py`    |
| `horus.middleware.workflow`  | `middleware/workflow.py` `WorkflowMiddleware` | `horus_builtin/middleware/workflow_time.py` |

## The runtime layer model (what plugs into what)

A **Workflow** orchestrates **Tasks** over a DAG. Each Task carries four plugins:

- a **Runtime** — *what* to run (a command string, a Python function, a script);
- an **Executor** — *how* to run it (shell over the target channel, in-process
  `exec`, in-memory function call). An executor declares which runtimes it accepts
  via its `runtimes` `ClassVar`; the task validates compatibility at construction.
- a **Target** — *where* it runs (local, SSH, cloud). The target provides the
  agentless **channel** primitives (`run_command`, `put_file`/`get_file`, detached
  `launch`/`poll`) so the same executor code works local or remote.
- **Artifacts** — the file-backed **inputs/outputs**. Artifact *existence* (and
  hash) is the source of truth for task completion and incremental skipping.

Cross-target input movement is a **Transfer strategy**, keyed by the
`(source_kind, dest_kind)` target pair. See `references/architecture.md` for the
full boot/run flow, the event bus + live logs, and the detach/channel layer.

## Gotchas (this repo's conventions)

- **Register the module, not the class.** Entry points point at
  `"my_pkg.targets.ssh"`, not `"my_pkg.targets.ssh:SSHTarget"`. Registration is a
  side effect of importing the module.
- **`kind` must be a non-empty default** on concrete classes (`kind: str = "ssh"`).
  A missing/empty key raises at import; a duplicate `kind` in the same domain
  raises `DuplicatedRegistryKeyError`.
- **Load workflows through the base/root class**, never a concrete subclass —
  `BaseWorkflow.from_yaml(...)` dispatches on `kind`; `HorusWorkflow.from_yaml(...)`
  would force one concrete type and defeat the registry.
- **Do not override the `@final` public methods** (`run`, `execute`,
  `setup_runtime`, `dispatch`, `transfer`, `reset`). Implement the `_`-prefixed
  hook instead (`_run`, `_execute`, `_setup_runtime`, `_dispatch`, `_transfer`,
  `_reset`). The public method drives status/middleware/events around your hook.
- **Abstract/intermediate bases don't register.** Set `add_to_registry = False` on
  a shared abstract base you don't want instantiated (abstract classes are skipped
  automatically anyway).
- **Product registries differ.** `transfer` and `interaction_renderer` have no
  `kind`; they declare `handles_*` `ClassVar`s and must inherit
  `AutoRegistryProduct` **before** `AutoRegistry`. See `references/registry.md`.
- **House style:** Python ≥3.13, Pydantic v2, PEP 695 generics (`class Foo[T]`),
  ruff line-length **79**, double quotes, Google docstrings, **`print()` is banned
  (T201)** — log via `from horus_runtime.logging import horus_logger`. mypy runs
  `--strict`. Wrap user-facing strings with `from horus_runtime.i18n import tr as _`.
- **Docs policy:** any change a user/plugin/GUI can observe requires a linked
  `horus-docs` PR (enforced by `.github/workflows/docs-check.yml`).

## References & templates

- `references/architecture.md` — layers, boot/run flow, event bus, channel/detach.
- `references/registry.md` — `AutoRegistry`, `AutoRegistryProduct`, `AutoMiddleware`
  internals and the exceptions they raise.
- `references/plugin-catalog.md` — per-domain base class, abstract signatures, and
  the built-in to copy.
- `assets/plugin_template.py` — a ready-to-edit plugin skeleton.
- `assets/entry_points_snippet.toml` — the entry-point table to paste into
  `pyproject.toml`.
