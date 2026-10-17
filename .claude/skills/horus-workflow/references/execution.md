# Execution semantics

What actually happens when a workflow runs: how the DAG is scoped, how the
scheduler dispatches tasks, how tasks are skipped, how inputs move between
targets, and how failures and shared machines are handled. Engine code lives in
`src/horus_runtime/`; the scheduler in `src/horus_builtin/workflow/scheduler.py`;
DAG utilities in `src/horus_builtin/workflow/dag.py`.

## Scope

`HorusWorkflow._run(trigger_id)` calls `run_schedule(workflow, trigger_id)`.

1. **Dependencies** come from edges (plus implicit ones for tasks added mid-run):
   `build_dependencies` maps each task to the tasks that must finish first.
   Edges whose source is a root artifact (`artifact-<id>`) add no ordering.
   Ordering-only and conditional edges still add ordering. No edges ⇒ all tasks
   independent.
2. **Scope** = `trigger_scope(trigger, deps)`: the trigger, its descendants,
   and the ancestors of **every** descendant (so a join's other parents run
   too). Unrelated branches are excluded.
3. Scope and dependencies are recomputed every loop iteration, so tasks added
   mid-run (`add_task`/`add_edge`/`expand`, map/sub/loop expansion) are picked
   up. A task added from inside a running task with no incoming task edge is
   gated behind its creator.

Trigger defaults to the first task (`workflow.tasks[0].id`). An unknown trigger
raises `UnknownTaskError`.

## Concurrent ready-set scheduler

Each iteration, every in-scope task whose dependencies are all completed is
**ready**, and all ready tasks are dispatched at once (sorted by id), bounded by
`max_concurrency` (unset = 8, `0` = no limit). The loop then waits for the
first completion and repeats.

Per task (`execute_task`):

1. **Branch gate**: if no incoming edge is live (see Conditions), the task is
   `SKIPPED` (`inactive`) without touching its inputs.
2. **Lease slot**: a task on a releasable shared target waits for the target's
   `member_cap` slot.
3. **Placement**: `PlacementManager.acquire` waits until the target's
   `location_id` has room for `task.resources` (only when `capacity` is declared
   for that location).
4. **Pool**: `TargetPool.acquire` takes a `max_concurrency` slot and hands back
   the declared target, or an `idle_copy()` of it when that instance is already
   busy (same location, so no transfer).
5. **Bind**: `target.bind(task)` so resource-aware targets can read
   `task.resources` while provisioning.
6. **Skip check before transfer**: `task.is_complete_before_transfer(...)`
   compares input digests taken at their source with the manifest. A hit marks
   the task `SKIPPED` (`complete`) without moving anything.
7. **Transfer** inputs (see below).
8. **Dispatch + wait**: `target.dispatch(task)` then `target.wait()`.
   `task.run()` checks completion once more, then runs the executor.

A failure in steps 4-7 (outside `task.run()`) also marks the task `FAILED`.

## Incremental skipping

`HorusTask.is_complete()` is true only when every declared output exists **and**
the manifest `.horus/<task_id>.json` in the target's working directory equals
the current fingerprint: a digest of every input, plus a hash of the runtime
and executor config and of their local files (scripts, environment files). The
manifest is written only after a successful run. Consequences:

- Editing an input, the command, or a script re-runs the task.
- A task with no outputs always runs. A missing or unreadable manifest means
  run.
- `workflow.reset()` / `task.reset()` deletes outputs and the manifest.
- `horus run --no-skip ID` / `--no-skip-all` set `skip_if_complete: false`.

Skip reasons (`task.skip_reason`, status `SKIPPED`):

| Reason            | Meaning                                   | Downstream    |
| ----------------- | ----------------------------------------- | ------------- |
| `complete`        | outputs exist and the manifest matches    | runs          |
| `inactive`        | no incoming edge was live (branch not taken) | inactive unless another edge is live |
| `upstream_failed` | an upstream task failed under `continue`  | skipped too   |

Task status flow: `IDLE → PENDING` (dispatched) `→ CONFIGURING` (executor
preparing: dirs, fingerprinting, provisioning, image pulls) `→ RUNNING →
COMPLETED`, or `SKIPPED` / `CANCELED` / `FAILED`. Workflow: `IDLE → RUNNING →
COMPLETED / CANCELED / FAILED`. All transitions are driven by the `@final`
`run()` methods; never set `status` yourself.

## Conditions

An edge `condition` (`kind: declarative`) reads a JSON document from an
upstream output, optionally follows a dotted `key`, and applies `op`: `eq`,
`ne`, `lt`, `le`, `gt`, `ge`, `in`, `not_in`, `contains`, `truthy`, `exists`.
A task is live if **any** incoming task edge is live (OR join); a task with no
incoming task edge is always live. Conditions on edges from a dead task are not
evaluated. Inactive tasks count as done, so a join below both branches of a
fork still runs. `wf.branch()` (Python) lowers to the same edges.

## Artifact transfer

Before dispatch, `transfer_artifacts(task, source_map)` makes each input present
on the task's target:

- The **source** comes from the `transfer: true` edge feeding that input: the
  producer's target, or `orchestrator_target` for a root input (no edge, or an
  `artifact-<id>` source). `OrchestratorTargetNotSetError` if that is unset.
  Pinned inputs (already materialized by map/loop) are skipped.
- Same `location_id` on both ends: no copy, the artifact is repointed.
- Otherwise the strategy is `BaseTransferStrategy.get_from_registry(source,
  dest)`; with none registered it falls back to
  `source.fallback_transfer()`, which is `GenericTransfer` by default (package
  on the source, stream through the orchestrator, unpack on the destination).
- The consumer input's `path` is then pointed at the materialized location, and
  a value input takes its source's `value`.

Reading an output from a released shared machine that was not evacuated raises
`ArtifactLostError`.

## Failure behavior

- `failure_policy: fail_fast` (default): the first failure cancels every task
  in flight and re-raises; the workflow ends `FAILED`.
- `failure_policy: continue`: the failure's descendants are marked `SKIPPED`
  (`upstream_failed`), other branches run to the end, then
  `WorkflowExecutionError` names every failed task and the workflow ends
  `FAILED`.
- Scope left with tasks that can never become ready, without a cycle, raises
  `StalledWorkflowError` instead of reporting success. A cycle raises
  `CyclicDependencyError`.
- Missing inputs raise `ArtifactDoesNotExistError` before the command runs; a
  non-zero exit raises `TaskExecutionError`. `CancelledError` gives `CANCELED`
  and kills the executor's process.
- Only one workflow may run per context (`OneWorkflowAtATimeError`).

## Shared targets and leases

Tasks with the same `{ref}` run on one target instance. For a **releasable**
target (e.g. the terraform plugin) the workflow's `LeaseManager` owns the
machine's life:

- **Busy** while a member runs; **holding** when members are pending but none
  runs, for the target's `hold_timeout_s`.
- When nothing needs it, or the hold expires, it **drains**: outputs that
  pending tasks still need are **evacuated** (default: copied to the
  orchestrator's `.evacuated/<task_id>/`), then it is **released**. A later
  member provisions it again.
- `keepalive()` is called every 300 s on busy or holding machines.
- `member_cap` limits members running at once.
- At the end of a run every machine is released; on failure or cancel, at
  once without evacuation.

Non-releasable targets (local, ssh, slurm) are never held or released.
