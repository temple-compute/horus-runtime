# Execution semantics

What actually happens when a workflow runs: how the DAG is scoped and ordered, how
tasks are skipped, how inputs move between targets, and the per-task dispatch
lifecycle. Engine code lives in `src/horus_runtime/`; DAG utilities in
`src/horus_builtin/workflow/dag.py`.

## From trigger to plan

`HorusWorkflow._run(trigger_id)` computes the run via
`execution_plan(tasks, trigger_id, edges)`:

1. **Dependencies** come only from edges: `build_dependencies` maps each task to
   the set of tasks that must finish before it (an edge's `source` task precedes
   its `target` task). Edges whose source is a root artifact (`artifact-<id>`) are
   root inputs and add no ordering. No edges ⇒ all tasks independent.
2. **Scope** = `ancestors(trigger) ∪ descendants(trigger)` — the trigger, every
   task that (transitively) depends on it, and every upstream task needed to run
   those. Unrelated branches are excluded entirely.
3. **Order** = `topological_sort` (Kahn's algorithm) over that scope. Ties break
   deterministically (a heap on task id). A cycle raises `CyclicDependencyError`;
   a trigger not in the task list raises `UnknownTaskError`.

Trigger defaults to the first task (`workflow.tasks[0].id`) when not given.

## Incremental skipping

Ordering selects *candidate* tasks; each still decides at run time whether to
execute. `BaseTask.run` (`@final`) skips a task — status `SKIPPED` — when
`skip_if_complete` is true (default) and `is_complete()` returns true. For
`HorusTask`, `is_complete()` is "all declared `outputs` exist" (and `False` when
there are no outputs, so an output-less task always runs). Net effect: re-running
a workflow only re-executes tasks whose outputs are missing. `workflow.reset()`
(or `task.reset()`) deletes outputs to force a full re-run.

Status flow per task: `IDLE → PENDING` (on dispatch) `→ RUNNING → COMPLETED`, or
`SKIPPED` / `CANCELED` / `FAILED`. Workflow: `IDLE → RUNNING → COMPLETED /
CANCELED / FAILED`. All transitions are driven by the `@final` `run()` methods —
never set `status` yourself.

## Artifact transfer

Before dispatching a task, `transfer_artifacts(task, source_map)` ensures each
input is present on the task's target:

- The **source** of an input is resolved from the edges (`_build_source_map`): if
  an edge feeds it from a producer's output, that producer's target is the source;
  otherwise it's a root input sourced from `orchestrator_target`
  (`OrchestratorTargetNotSetError` if that's unset when needed).
- The strategy is looked up by target-kind pair:
  `BaseTransferStrategy.get_from_registry(source_target, dest_target)`
  (`TransferStrategyNotFoundError` if none is registered). For all-local workflows
  this is `LocalNoOpTransfer` — a no-op, since both share the filesystem.
- After transfer, the consumer input's `path` is pointed at the materialized
  location so command substitution resolves correctly.

To run across machines you add a target plugin plus a transfer strategy for the
`(source_kind, dest_kind)` pair — see the `horus-plugin` skill.

## Per-task dispatch lifecycle

For each task in the plan, `HorusWorkflow._run` does:

```python
task.target.bind(task)                       # associate before provisioning
await self.transfer_artifacts(task, source_map)
await task.target.dispatch(task)             # PENDING → schedule task.run()
await task.target.wait()                      # await completion
```

`dispatch` (`@final`) sets `PENDING`, runs target middleware, and `_dispatch`
schedules `task.run()` as an asyncio task. `task.run()` (skipping aside) calls the
executor, which renders the runtime and runs it over the target's **channel**. For
`ShellExecutor` that's `task.target.run_command(...)`, streaming stdout/stderr
live into the logs/TUI as the process produces them. Detached execution (jobs that
survive a dropped channel) and live-log streaming are covered in the
`horus-plugin` skill's `references/architecture.md`.

## Failure behavior

A task exception sets the task `FAILED` and propagates: `wait()` re-raises, the
workflow goes `FAILED`, and (via the CLI) the process exits non-zero.
`CancelledError` yields `CANCELED` and cancels the process group. Missing input
artifacts raise `ArtifactDoesNotExistError` before the command runs; a non-zero
command exit raises `TaskExecutionError`.
