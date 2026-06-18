# Design 0001 — Agentless targets, composable executors

| | |
|---|---|
| **Status** | M1 + M2 landed — M3+ roadmap |
| **Date** | 2026-06-12 (updated 2026-06-18) |
| **Scope** | `horus-runtime` core, `horus_builtin`, `horus_ssh` (separate repo), future SLURM/Docker support |

This document is both the architecture decision record and the implementation
plan. Milestones and issue drafts live at the bottom.

**Milestone status (2026-06-18):**
- **M1 — Channel foundation**: ✅ LANDED. Channel primitives on `BaseTarget`
  (`run_command`, `put_file`, `get_file`, `mkdir`), `ChannelProcess` handle,
  `LocalTarget` channel implementation, dispatch lifecycle hoisted into
  `BaseTarget`, `RemotePath` (`PurePosixPath`) semantics.
- **M2 — Executors drive the channel**: ✅ LANDED (core items).
  `ShellExecutor` drives `task.target.run_command` (M2.2 naming decision: keep
  `shell` kind for back-compat). Side-artifacts mkdir via channel (M2.3).
  `SSHTarget` fat-mode (`remote_runner`) deleted (M2.5 — drop option chosen).
  End-to-end demo and integration tests (M2.6): a 3-task workflow executes
  `script.py` inside a `python:3.13-slim` Docker container on a remote
  sshd+DinD host with **no Horus installed**, transfers `result.json` back via
  `SSHToLocalTransfer`, and consumes it locally — verified in
  `horus_ssh/tests/integration/test_e2e_agentless.py`.
- **M3+ — Layer system, SLURM, validation**: roadmap, not yet started.

---

## 1. Problem

Dispatching a task to a remote machine currently requires a full
`horus_runtime` installation there: `SSHTarget` serializes the task, uploads
`task.json`, and runs `python -m horus_ssh.remote_runner` so that
`task.run()` (executor, runtime, middleware, events) executes on the remote
host. Consequences:

- **Environment bootstrapping is the wrong abstraction.** HPC compute nodes
  routinely have no internet access; "sync the horus environment to the
  machine" cannot be a precondition for running `echo hello` remotely.
- **Scheduler integration does not fit.** SLURM (and PBS, LSF, …) is not a
  *place* (target) nor a single environment; modelling it as a target breaks
  the transfer-strategy matrix (exact `kind` lookup, no MRO fallback) and
  forces `LocalSlurmTarget`/`SSHSlurmTarget` subclass explosion.
- **Observability is degraded remotely.** Events emitted inside
  `remote_runner` go to the *remote* context bus and never reach the
  orchestrator; only an exit code and stdout/stderr come back.
- **Cancellation is broken remotely.** `SSHTarget.cancel()` SIGTERMs
  `remote_runner`; nothing converts the signal to asyncio cancellation, so
  executor cleanup never runs and shell children are orphaned (reparented and
  left running). Latent bug today, fatal once jobs hold cluster allocations.
- **Version skew.** The remote horus must be serialization-compatible with
  the orchestrator's (`BaseTask.model_validate_json` of the full task).

## 2. Decision (proposed)

Invert the placement of `task.run()`: it **always executes on the
orchestrator**. The layers become:

| layer | responsibility | identity |
|---|---|---|
| **Target** | *where* — an **agentless communication channel**: run a command, move a file. Plus location identity for transfers. | `location_id` |
| **Executor** | *how* — owns the **execution lifecycle** via a native handle, drives the channel | PID / SLURM job ID / container ID |
| **Layer** (new) | *in what context* — transparent command/script wrappers with no lifecycle of their own (conda, modules, env vars, foreground docker, srun) | none (by design) |
| **Runtime** | *what* — renders the innermost command/script | — |
| **Transfers** | artifact movement between locations | unchanged, keyed on target kinds |

A task therefore reads as a composition, e.g. `slurm( conda( python ) )`:
runtime renders `python train.py`; the conda layer wraps it in activation;
the SLURM executor renders the sbatch script, submits it through the channel,
and polls/cancels by job ID.

### 2.1 Target = channel

```python
class BaseTarget:
    # unchanged: location_id, working_directory, access_cost  (transfer machinery)

    # new channel primitives (no horus required on the remote side, ever):
    async def run_command(self, cmd, *, cwd=None, env=None) -> ChannelProcess
    async def put_file(self, content_or_path, remote_path) -> None
    async def get_file(self, remote_path) -> bytes
    async def mkdir(self, path) -> None
```

`ChannelProcess` is a small handle: stdout/stderr access, `returncode`,
`wait()`, `signal()/kill()`. `LocalTarget` implements the primitives with
`asyncio.create_subprocess_*` (spawned via `setsid`, killed by process
group); `SSHTarget` with asyncssh sessions + SFTP over one persistent,
keepalive-protected connection.

The `dispatch/wait/cancel/get_status` lifecycle becomes uniform — what
`LocalTarget` does today (`asyncio.create_task(task.run())`) is hoisted into
`BaseTarget` as the default for every target. `SSHTarget` deletes
`remote_runner`, `_ensure_remote_runtime`, task serialization, and its
process-tracking attributes.

### 2.2 Executor = lifecycle owner (exactly one, outermost)

```python
class BaseExecutor:
    layers: list[BaseExecLayer] = []          # innermost-last (see open decision D2)

    async def _execute(self, task):
        script = Script(command=await task.runtime.setup_runtime(task))
        for layer in reversed(self.layers):
            await layer.prepare(channel, task)     # staging; may fail cleanly
            script = layer.wrap(script)            # pure transformation
        handle = await self._submit(channel, script)   # native handle
        try:
            await self._wait_terminal(handle)          # poll native state
        except asyncio.CancelledError:
            await self._cancel_native(handle)          # kill -- -PGID / scancel / docker stop
            raise
```

Concrete executors: `ProcessExecutor` (handle = PID/PGID — the channel-driven
successor of `ShellExecutor`), `SlurmExecutor` (job ID), `DockerExecutor`
(detached container ID). In-process executors (`python_exec`, `python_fn`)
remain valid but are restricted to in-process-capable targets (§2.5).

### 2.3 Layers = transparent wrappers

```python
class BaseExecLayer(AutoRegistry, entry_point="exec_layer"):
    async def prepare(self, channel, task) -> None: ...
    def wrap(self, inner: Script) -> Script: ...
```

`Script` is a deliberately small structure — `prologue: list[str]`,
`command: str`, `epilogue: list[str]`, `traps: list[str]` — so layers compose
without parsing each other's output, and the executor renders the final
artifact (plain script, sbatch file, container entrypoint) from it.

**Preparation has a placement.** `prepare()` runs through the orchestrator's
channel, i.e. on the *submit host*. That is correct only for shared resources
(conda env on a shared filesystem). Anything that must happen where the
command finally runs (e.g. `docker pull` inside a SLURM job) must be emitted
as *prologue lines* instead. Each layer chooses per resource.

**Environment preparation strategies** (offline-first, for python-ish
layers): `existing` (activate a pre-existing path / `module load`; fail
cleanly if absent), `archive` (push a prebuilt conda-pack/venv tarball
through the channel and untar — safe for airgapped nodes), `resolve`
(`pip`/`conda` install; requires internet; fails with a clean
`TaskExecutionError`).

### 2.4 Cancellation invariant

Cancel signals **only the executor's native handle**. This is correct iff
every layer ties all its resources to the wrapped process tree. Design rule:

> A layer must not create anything that outlives its wrapped process. If it
> cannot guarantee that, it registers a cleanup trap — or it is not a layer,
> it is an executor.

Known leak classes and their mitigations:

1. *Docker client/daemon split* — killing the `docker run` client does not
   stop the container. Foreground `DockerLayer` must use `--rm --init` and
   emit `trap 'docker stop $CID' TERM INT` (`scancel` sends TERM before KILL,
   so the trap gets its window).
2. *Daemonizing children* — `ProcessExecutor` spawns with `setsid` and
   cancels the process group; the layer contract forbids daemonizing.

SLURM is the friendly case: `scancel` signals the whole job step.

### 2.5 Validation (at construction, like `_validate_runtime_compatibility`)

- Executor ↔ target capability: executors declare needs (`in_process`,
  required binaries such as `sbatch`); targets declare channel capabilities.
  `python_fn` + `ssh` fails at model validation with a clear message.
- Exactly one lifecycle owner: at most one queueing/detaching semantic per
  chain, and it is the executor. `slurm` executor + detached-docker layer is
  rejected.
- Layer ↔ runtime compatibility mirrors `executor.runtimes`.
- Failure attribution: generated fragments use `set -euo pipefail`
  discipline; epilogues must not mask the command's exit code. Layers do not
  inspect or translate exit codes (v1).

### 2.6 What happens to fat mode

The current remote-runner behavior ("run full horus machinery on the remote
machine") stops being the tax every SSH task pays. Options (open decision
D1): drop it entirely, or quarantine it as an opt-in `horus_remote` executor
that serializes the task and runs `python -m …` through the channel — the
one executor whose declared environment need is "a horus installation".

## 3. Consequences

Wins:

- Remote execution of commands/scripts/SLURM requires **no python and no
  internet** on the remote side — only the binaries the executor declares.
- The SIGTERM/orphan cancellation bug class disappears structurally: there is
  no remote python intermediary; `CancelledError` reaches the orchestrator-
  side executor directly, which cancels through the native handle.
- All events/middleware/status on one bus — uniform observability.
- No serialization version-coupling between orchestrator and remote hosts.
- SLURM/Docker/PBS each land as one executor class; targets and the transfer
  matrix never grow.

Costs / changes to watch:

- All filesystem operations in core executor paths must go through the
  channel (`side_artifacts_dir.mkdir()`, `collect_side_artifacts()` iterate
  local `Path`s today — correct only because the code currently runs
  remotely). Remote paths become `PurePosixPath`.
- The orchestrator must stay connected for the task's duration (same as
  today, so no regression). Dispatch-and-disconnect/`recover()` becomes a
  per-executor story (trivial for SLURM via `sacct`; `nohup`+pidfile pattern
  for plain processes). Deferred to M5.
- `horus_ssh` shrinks substantially (remote_runner removed or quarantined) —
  packaging/commercial implications to weigh.
- Behavior change: `ssh` + `shell` tasks stop running remote middleware. This
  is intended (uniform orchestrator-side machinery) but is a breaking change
  for anyone relying on remote-side hooks.

## 4. Open decisions (annotate before M1 starts)

- **D1 — Fat mode fate**: drop `remote_runner` entirely, or keep as opt-in
  `horus_remote` executor? *(affects M2.5)*
- **D2 — Layer composition shape**: `layers: list[...]` field on the
  executor (lean; recommended) vs. recursive nesting
  (`conda.inner = python`) that mirrors `slurm(conda(python))` notation but
  reintroduces outermost-ambiguity. *(affects M3.1)*
- **D3 — `Script` richness**: prologue/command/epilogue/traps proposed;
  resist a full AST. *(affects M2.1)*
- **D4 — Where layer classes live**: `horus_builtin.layer.*` in-tree
  (zero-dep) vs. separate plugin packages per ecosystem. *(affects M3)*
- **D5 — Naming**: "Layer" vs "Environment" vs "Wrapper" for the new
  abstraction; "ProcessExecutor" vs keeping the `shell` kind. *(affects M2/M3)*

---

# Implementation plan

Issues are numbered `M<milestone>.<n>` for stable cross-reference inside this
document. GitHub numbering happens only when a milestone is ratified and its
issues are published. Repo column: `core` = this repo, `ssh` = the
`horus_ssh` repo. Each milestone is independently shippable; behavior changes
are flagged.

## Milestone 1 — Channel foundation (targets become agentless)

> Goal: targets expose channel primitives; nothing consumes them yet. Zero
> behavior change.

### M1.1 — `ChannelProcess` + channel primitives on `BaseTarget` `[core]`
Define the abstract channel API (`run_command`, `put_file`, `get_file`,
`mkdir`) and the `ChannelProcess` handle (stdout/stderr, `returncode`,
`wait()`, `signal()`/`kill()`). Decide and document `cwd`/`env` semantics and
text-vs-bytes streams.
- [ ] Abstract methods + docstrings on `BaseTarget` (or a `Channel` mixin —
      decide here)
- [ ] `ChannelProcess` protocol/ABC
- [ ] No existing behavior touched

### M1.2 — `LocalTarget` channel implementation `[core]`
- [ ] `run_command` via `asyncio.create_subprocess_shell` with `setsid`
      (process-group spawn)
- [ ] `kill()` kills the process group (`kill -- -PGID`)
- [ ] `put_file`/`get_file`/`mkdir` via local fs
- [ ] Unit tests incl. group-kill of a child-spawning command

### M1.3 — `SSHTarget` channel implementation `[ssh]`
- [ ] Persistent connection, opened lazily, with configurable
      `keepalive_interval`/`keepalive_count_max` and connect/login timeouts
- [ ] `run_command` via asyncssh sessions (multiple sessions per connection
      for parallel tasks); `put_file`/`get_file` via SFTP
- [ ] "Connection lost" distinguishable from "command failed" in errors
- [ ] Unit tests against a dockerized sshd

### M1.4 — Hoist default dispatch lifecycle into `BaseTarget` `[core]`
`dispatch` = `asyncio.create_task(task.run())`, `wait`/`cancel`/`get_status`
as in today's `LocalTarget`, as the default for all targets. `LocalTarget`
keeps only `location_id`/`access_cost`/channel code.
- [ ] Default lifecycle in `BaseTarget` (non-abstract now)
- [ ] `LocalTarget` slimmed; behavior identical (existing tests pass)

### M1.5 — Remote path semantics `[core]`
`working_directory`/`working_dir`/`side_artifacts_dir` for remote targets are
POSIX paths on another machine; today they are `Path` (breaks on a Windows
orchestrator, conflates local/remote).
- [ ] Audit all `Path` uses in task/executor/target core
- [ ] Introduce `PurePosixPath` (or a `RemotePath` alias) for target-side paths
- [ ] Document the rule: target-side paths are never `os`-touched locally

## Milestone 2 — Executors drive the channel (de-fat the SSH path)

> Goal: `ssh` + `shell` works with **no horus on the remote host**.
> ⚠ Behavior change: remote tasks stop running remote-side middleware/events
> (now uniform orchestrator-side).

### M2.1 — `Script` model + renderer `[core]`
- [ ] `Script(prologue, command, epilogue, traps)` (see D3)
- [ ] Renderer to a POSIX shell script with `set -euo pipefail` discipline;
      epilogue cannot mask the command's exit code
- [ ] Golden tests

### M2.2 — `ProcessExecutor`: channel-driven successor of `ShellExecutor` `[core]`
- [ ] `_execute` = render Script → `target.run_command` → handle = ChannelProcess
- [ ] `CancelledError` → group-kill via handle, re-raise
- [ ] Exposes `SIDE_ARTIFACTS_DIR` env as today
- [ ] Decide kind naming/back-compat with `shell` (D5)
- [ ] Works identically with `local` and `ssh` targets (same tests run twice)

### M2.3 — Side artifacts through the channel `[core]`
`BaseExecutor.execute()` does local `mkdir`; `collect_side_artifacts()`
iterates a local `Path` — both wrong once execution is remote-but-orchestrated.
- [ ] `mkdir` via `target.mkdir`
- [ ] Collection via channel listing + `get_file`, or delegate to transfer
      strategies (decide here)
- [ ] Works for `local` and `ssh`

### M2.4 — Executor ↔ target capability validation `[core]`
- [ ] Executors declare `in_process: bool` and `required_binaries: list[str]`
- [ ] Targets declare channel capabilities
- [ ] `python_exec`/`python_fn` + remote target → clear validation error at
      task construction
- [ ] Optional cheap preflight (`command -v sbatch`) at dispatch, behind a flag

### M2.5 — Decide and execute fat-mode fate (D1) `[core+ssh]`
- [ ] If dropped: delete `remote_runner`, `_ensure_remote_runtime`, task
      payload upload from `horus_ssh`
- [ ] If quarantined: `horus_remote` executor wrapping today's behavior,
      declared need = horus installation; SIGTERM→cancellation fix included
- [ ] Migration notes for existing `ssh+shell` users (behavior change above)

### M2.6 — e2e: bare remote host `[ssh]`
- [ ] Docker sshd container **without python** in CI
- [ ] `ssh` + process executor: complete / fail / cancel (no orphaned children
      — asserts the group-kill design)
- [ ] Artifact round-trip with existing transfer strategies

## Milestone 3 — Layer system (composition)

> Goal: `conda( python )` style wrapping works under any executor.

### M3.1 — `BaseExecLayer` + composition in `BaseExecutor` `[core]`
- [ ] `prepare(channel, task)` / `wrap(Script) -> Script`; AutoRegistry entry
      point `exec_layer`
- [ ] `layers` field + documented ordering (D2)
- [ ] Layer ↔ runtime compatibility filter (mirrors `executor.runtimes`)

### M3.2 — Chain validation invariants `[core]`
- [ ] Exactly one lifecycle owner (the executor); detaching/queueing layers
      rejected
- [ ] Layer resource rule documented: nothing outlives the process tree, or
      register a trap
- [ ] Clear error messages with the offending chain spelled out

### M3.3 — `EnvVarLayer` + `ModuleLayer` `[core]`
The two trivial layers that prove the contract.
- [ ] `EnvVarLayer(vars)` → prologue exports
- [ ] `ModuleLayer(modules)` → `module load …` prologue, `existing`-style
      failure if unavailable
- [ ] Unit tests on wrapped Script output

### M3.4 — `CondaLayer` (and/or venv) with offline-first preparation `[core]`
- [ ] Strategies: `existing` (activate path), `archive` (push conda-pack/venv
      tarball via channel, untar on shared fs), `resolve` (install; clean
      failure without internet)
- [ ] Placement rule honored: shared-fs staging via `prepare()`, node-local
      needs via prologue
- [ ] Validation: `resolve` + offline flag fails at construction

### M3.5 — Python runtime → shell-renderable bridge `[core]`
So `conda( python_runtime )` composes: materialize the code to a script file
in `task.working_dir` (via channel `put_file`) and render
`<interpreter> <script>` as the command.
- [ ] New runtime or extension of existing python runtimes
- [ ] Artifact-path placeholders: same `.format` contract as `CommandRuntime`

## Milestone 4 — SLURM executor

> Goal: `slurm( conda( python ) )` and plain `slurm( command )` on local and
> SSH channels. Compute nodes need nothing installed.

### M4.1 — `SlurmJobOptions` + sbatch rendering `[core or plugin — D4]`
- [ ] Typed options (partition, account, qos, time_limit, nodes, ntasks,
      cpus_per_task, mem, gres, constraint, job_name, extra_directives)
- [ ] Render Script → sbatch file: directives + `--export=NONE` default +
      `--chdir`/`--output`/`--error` into the task working dir /
      side-artifacts dir
- [ ] Submission script preserved as a side artifact; golden tests

### M4.2 — `SlurmExecutor` lifecycle `[core or plugin]`
- [ ] Submit `sbatch --parsable` via channel → handle = job ID
- [ ] Poll `squeue`, terminal state via `sacct` with `scontrol show job`
      fallback; configurable interval + gentle backoff
- [ ] State mapping: COMPLETED→ok; FAILED/TIMEOUT/OUT_OF_MEMORY/NODE_FAIL/
      BOOT_FAIL/DEADLINE/PREEMPTED→`TaskExecutionError` (state, ExitCode,
      stderr tail); external CANCELLED→`CancelledError`
- [ ] `CancelledError` → `scancel`, re-raise
- [ ] `required_binaries = [sbatch, squeue, scancel]`

### M4.3 — Queue-state events `[core]`
- [ ] Event (task id, job id, raw scheduler state) on every transition from
      the poll loop, following `RuntimeEvent`/`HorusTaskEvent` patterns
- [ ] Evaluate `TaskStatus.QUEUED` (touches the core enum + all consumers —
      separate decision, do not block M4)

### M4.4 — Stubbed-CLI unit tests `[same as M4.1/2]`
- [ ] Fake `sbatch`/`squeue`/`sacct`/`scancel` on a temp PATH driven by a
      state file
- [ ] Full state-mapping table; cancel calls `scancel` exactly once; sacct→
      scontrol fallback; sbatch failure surfaces stderr

### M4.5 — SLURM-in-Docker e2e rig `[core]`
- [ ] compose: slurmctld + slurmd + sshd on the login container; horus
      installed **only** where the orchestrator runs — never on compute
- [ ] Matrix: {local, ssh} × {complete, fail, cancel}; cancel leaves an empty
      queue
- [ ] `slurm( conda(existing)( python ) )` composition case

### M4.6 — Docs + examples `[core]`
- [ ] Composition guide: target × executor × layers × runtime, with the
      env-requirements matrix ("compute nodes need nothing")
- [ ] Runnable YAML examples: `slurm(command)`, `slurm(conda(python))`,
      both `target: local` (login node) and `target: ssh`

## Milestone 5 — Docker + durability (post-MVP, order negotiable)

### M5.1 — `DockerExecutor` (detached; handle = container ID) `[plugin?]`
- [ ] `docker run -d --rm` via channel; poll `docker inspect`; cancel
      `docker stop`; logs → side artifacts

### M5.2 — `DockerLayer` (foreground, transparent) `[plugin?]`
- [ ] Wraps inner Script into `docker run --rm --init … bash -c '…'`
- [ ] Emits `trap 'docker stop $CID' TERM INT` (client/daemon leak)
- [ ] e2e: `slurm( docker( command ) )` cancel kills the container

### M5.3 — Per-executor recovery / dispatch-and-disconnect `[core+ssh]`
- [ ] Journal `{handle, kind, state}` in the task working dir at submit
- [ ] `SlurmExecutor.recover()` via `sacct` (easy win); `ProcessExecutor` via
      pidfile (best-effort)
- [ ] Decide where `recover()` lives now that targets no longer own execution

### M5.4 — Connection-drop resilience `[ssh]`
- [ ] Reconnect-and-reattach path on channel failure mid-poll (SLURM first)
- [ ] Distinguish task failure from channel failure in workflow error handling

---

## Suggested first PR sequence

M1.1 → M1.2 → M1.4 (pure additions, zero risk) → M2.1 → M2.2+M2.3 (the
behavior change, one reviewable unit) → M2.6 (proof: bare remote host). Then
ratify D1 before touching `horus_ssh` (M2.5), and D2/D4 before M3.
