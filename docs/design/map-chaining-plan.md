# Map chaining: per-item flow between horus_map stages

## Context

The Horus vs Nextflow bench (tc-os #655, rig `scripts/bench/nfcore` on `benchmarking`) shows the faithful rnaseq port ties with Nextflow on EC2 (265 s vs 265 s) and trails the fused port (`rnaseq-tuned`, 235 s). Nextflow moves each sample to its next step as soon as that sample is ready; a Horus map waits for all of its clones.

What exploration found, which shapes this plan:

1. **The rnaseq port has no map-over-map edge today.** Every map iterates a sample-list folder written by a plain task (`samples`, `trim_pass`, `strand_pass`, `map_pass`, ...). Downstream maps take the upstream map's *whole* folder as an ordinary input and glob `${bbsplit}/*/$${id}_primary_1.fastq.gz`. So the barrier before STAR is the sum of:
   - the BBSplit map barrier;
   - three serial steps (`lint_bbsplit`, `subsample`, `salmon_strand`);
   - a gather task (`set_strandedness`).

   One laptop rep: the first BBSplit sample was ready at 21:58:24.8, and `STAR_ALIGN` started at 21:59:02.9, **38 s later**. Gathers `filter_trimmed` and `filter_mapped` cut the same kind of barrier elsewhere.
2. Chaining only `over` is therefore not enough. STAR needs, per sample, both the BBSplit reads and the strandedness. A chained map needs **per-item side inputs from other maps in the chain** (fan-in by item), and the port's per-sample filters have to become per-item maps that drop samples.
3. Runtime facts:
   - A map runs its own clones (`dispatches_children`, its own `TargetPool`, shared `wf.placement` and `_FairSlots`).
   - The only owner link is the id pattern `id[\d+]`, used by the runtime, `tc-models` and the frontend.
   - No event carries task status. The UI sees clones only through full snapshot pushes (`tc-plugins/task_status_report.py`, about 1/s).
   - `horus sanitize` only promotes root inputs; validation is whatever `from_yaml` and pydantic do.
   - Maps currently hold an outer workflow pool slot while their clones run.

Goal: map B's clone for item X starts once every chained input of X is ready (A[X] succeeded), with strict validation and a UI that shows waiting and dropped items through the chain.

## Concepts and rules (proposed)

- **Item space.** Each map has an item space: the `(task, output)` the head map's `over` edge comes from. A map whose `over` is fed by another map's output inherits that map's space and its slot names (this already happens via `_over_is_map_output`). Two maps over the same `samples` output share a space.
- **Items match by slot name, never by position.** Slot names are fixed at the head (zero-padded indices) and inherited down the chain. A drop leaves a gap, so keys never shift.
- **Chained map.** A map with `chain: true`. Each clone X waits for slot X of every chained input:
  - the `over` input, plus any `zip` input fed by a map;
  - the wait happens before it takes any pool slot, placement or lease slot.
- **Per-item side inputs (`zip`).** `zip: [{input_id, as}]` binds slot X of another map's folder to clone X as `$<as>`. It is allowed with or without `chain`. Without `chain` it is a safe by-key pairing after the barrier, and it replaces the `*/$${id}` glob.
- **Fan-out inside a chain.** One map can feed several chained maps; each subscribes to the same per-slot signals.
- **Mixed consumers.** A plain task or a non-chained map consuming A's folder still waits for all of A, as today.
- **Not in scope.** Changing the key set inside a chain (1 item to N, or N to 1, e.g. `cat_fastq` over `multi_run`). That map is a new chain head.

### Failure and drop semantics

| Upstream event on item X | Chained downstream clone B[X] |
|---|---|
| A[X] completes or is skipped as complete | becomes ready; it skips on its own manifest if unchanged |
| A[X] fails, A has `on_item_failure: drop` | `SKIPPED`, new `skip_reason: upstream_dropped`; no slot X in B's folder; the same happens in every later chained map |
| A[X] fails, A has `on_item_failure: fail` | A is failing. `fail_fast`: the run cancels, as today. `continue`: B starts no new clones, running B clones finish, then B ends `SKIPPED upstream_failed`; finished clones keep their status, as `_mark_blocked` already does |
| A fails before it has its slot list | B never started; normal blocking, as today |
| A[X] output lost with its machine | B[X] pulls through `EdgeSource(task_id=A[X])`, so `_recover_if_lost` and lease recovery apply; A's `_rerun_lost` still runs |
| A skipped whole (map manifest matches) | no streaming; B runs the normal path, all slots present |

### Incremental skip and re-runs

- A chained B dispatched early **skips its own map-level completion check**, because A's folder is still partial. Its clones' manifests do the per-item skip, keyed by the item digest, which is `EdgeSource.digest` of A[X]'s slot.
- B writes its map manifest after it settles, with the final input digests, by re-taking the fingerprint. If anything was dropped it removes the manifest instead, as today.
- If one input item changes, only that item's chain re-runs; the other clones skip.
- B still removes stale slots that are no longer in the expected set (existing behaviour, `TestSlotNames`).

## Runtime design (horus-runtime, release v0.9.0)

1. **Item stream on MapTask** (`horus_builtin/workflow/map.py`). It lives in a private attribute `_stream`, so `BaseWorkflow` gains no new API.
   - `open(slots)` is called right after `wf.expand(...)`, once the slot list is known. A chained map opens its stream with the inherited slots at its own start, so a 3-map chain opens all three almost at once.
   - `ready(slot, target, artifact, clone_id)`, `dropped(slot)` and `failed(exc)` resolve per-slot futures.
   - Each clone run is wrapped (`_run_clone`) so that it calls `ready(...)` or `dropped(...)` as soon as that clone settles. Today that only happens in `_settle` at the end. Under `drop`, a failed clone counts as dropped immediately; if every clone then fails, the map fails, as today.
2. **MapTask fields**:
   - `chain: bool = False`
   - `zip: list[MapOver] = []`
   - `streamed_input_ids()`, which returns the `over` input and the zip inputs fed by a map when `chain` is set.
3. **Chained `_execute`.**
   - Read the expected slots from the source map's stream; do not call `items()` on a partial folder.
   - Build every clone up front. These are the placeholders: `IDLE` in the snapshot.
   - Per clone: `await` every chained input's slot future, then `execute_task(...)`.
   - The clone's `source_map` entry for the item is `EdgeSource(A.target, A[X].outputs[0], task_id=A[X].id, report=False, digest=...)`, not B's own target, so cross-machine chains transfer per item.
   - Upstream dropped: mark the clone `SKIPPED/upstream_dropped`, emit one event, and add the slot to `_dropped_slots`. B's own drop message then has two parts: B's own failures and "N item(s) dropped upstream".
4. **Scheduler** (`horus_builtin/workflow/scheduler.py`). Three changes:
   - **Readiness:** task T is ready when its non-streamed deps are `completed` and each streamed dep is either completed or has an open stream. This uses a `BaseTask` hook, `streamed_dependencies(workflow) -> set[str]` (default empty).
   - **Wake-up:** `asyncio.wait` also waits on a stream-opened future of any running source that a not-yet-ready task streams from. Today the loop wakes only when a wrapper finishes.
   - **Continue policy:** when a failure blocks descendants, cancel any of them that are still running (a chained map waiting on that source), then mark them as today.
5. **Capacity and placement.**
   - A `dispatches_children` task no longer takes the **outer** `TargetPool` semaphore. It already takes no placement and no lease slot. Without this, 20-plus chained maps waiting at once would use up the workflow's `max_concurrency` (16 in rnaseq) and starve plain tasks.
   - The per-map `max_concurrency`, shared `wf.placement` resources and `_FairSlots` round-robin by map group are unchanged.
   - The upstream wait comes before every slot, so a waiting clone holds nothing.
   - Lease: B must stay a pending consumer of A's machine until its last clone has pulled. Call `leases.inputs_transferred(B)` for streamed inputs only after `_settle`. This only matters when A and B are on different releasable machines.
   - `workflow.transfer_artifacts(B)` and `source_digests(B)` skip `streamed_input_ids()`, the same way pinned or unfingerprinted inputs are skipped, through the same hook.
6. **Events.**
   - Add `SkipReason.UPSTREAM_DROPPED = "upstream_dropped"` in `core/task/status.py`. Like `COMPLETE`, it counts as satisfied for the map, so it never blocks.
   - Add `data` to the map's events: `{"map": {"chain": [A_id], "slots": [...]}}` on stream open, and `{"dropped": [slots]}` on drops. The UI keeps reading snapshot statuses; `data` is there for logs and MCP `get_run_events`.
   - TUI: placeholders render in the existing fan-out tree; dim `upstream_dropped` as "dropped".
7. New `_()` strings need `make babel-refresh` plus Spanish entries, because CI runs `babel-check`.

## Validation (exact messages)

The rules are pydantic validators: MapTask `model_validator(mode="after")`, plus a `HorusWorkflow` validator for the cross-task rules, kept in builtin, not core. They run on every load: `horus run`, `horus sanitize` (which loads the YAML), backend save, and MCP edits. Run-time checks are kept for state that can't be known statically.

Load time:
- V1 `zip` input not among inputs: reuse `"Input artifact '%(id)s' not found in task inputs."`
- V2 `zip` input not a folder: reuse `"Input artifact '%(id)s' is not iterable."`
- V3 `as` collides with a port or another binding: `"Map task '%(id)s' binds '%(item)s' twice."`
- V4 `chain: true` but `over` is not fed by a map: `"Map task '%(id)s' is chained, but '%(input)s' does not come from a map."`
- V5 `zip` input is not fed by a map output (a root, a plain task, or `transfer: false`): `"Map task '%(id)s' pairs '%(input)s' by item, but it does not come from a map."`
- V6 item space mismatch: `"Map task '%(id)s' pairs '%(input)s' by item, but '%(source)s' maps over different items than '%(over_source)s'."`
- V7 chained body reads a whole streamed folder (`placeholder_roots` contains the `input_id` of `over` or a zip input, or `task`): `"Map task '%(id)s' is chained, so it cannot read all of '%(input)s'. Use '$%(as)s' for this item."`
- V8 chained map with a non-command runtime: `_body_may_read` is True, so V7 can't be proven. Allowed; the docs warn that the body must only read `$<as>`. See open question Q4.
- V9 a chained input has a second edge into the same map that delivers the whole folder: `"Map task '%(id)s' is chained to '%(source)s', so no other input can take all of its output."`

Run time:
- R1 expected slot missing upstream (a bug guard): `"Map task '%(id)s' expected item %(slot)s from '%(source)s', which has no such item."`
- R2 zip slot sets differ although the item space is the same (e.g. a hand-edited folder): `"Map task '%(id)s' found items in '%(input)s' that '%(over)s' does not have: %(slots)s."`

Frontend `mapOverIssues` mirrors V3 to V7 so the editor flags them before save.

## YAML surface (opt-in)

```yaml
- kind: horus_map
  id: star_align
  chain: true                                   # start each item when its inputs are ready
  over: {input_id: strandedness, as: strand}    # fed by map set_strandedness
  zip:
    - {input_id: bbsplit, as: reads}            # slot X of map bbsplit
  inputs: [*a_strandedness, *a_bbsplit, *a_star_idx, *a_genome]
  outputs: [*a_star_align]
  command: "... ${reads}/sample_primary_1.fastq.gz ... $(cat ${strand}/strand) ..."
```

Python: `MapTask(..., chain=True, zip=[MapOver(input_id="bbsplit", item_id="reads")])`.

## UI design (tc-os frontend)

The chain decision comes from YAML (`chain`, `zip`, the over edge's source kind), so the UI doesn't have to guess.

**Canvas edge.** An edge into a chained input renders as a "per item" edge: a solid double stroke with a small link glyph. Barrier edges stay as they are. Changes: `DeletableEdge.tsx`, plus a `chained` flag from `hierarchy.ts`.

```
 ┌───────────┐           ┌────────────┐          ┌────────────┐
 │ bbsplit ×8│══ ⛓ ════▶│ lint    ×8 │══ ⛓ ════▶│ star    ×8 │
 └───────────┘           └────────────┘    ┌════▶└────────────┘
       ╚═══════════════════ ⛓ (reads) ════┘
       │                                        ─── plain edge = waits for all
       └────────────────────────────▶ multiqc
```

**Map badge (run).** It adds a waiting count. Tooltip: "Starts each item when bbsplit finishes it."

```
 [▤ 3/8 · 37%]  [2 waiting]  [1 dropped]
```

**Clone deck, Table view.**
- New derived buckets `waiting` and `dropped upstream`. A placeholder is `IDLE` while any chained source clone of the same slot is not done.
- New column "From" (shown only when chained).

```
 Slot  Item      Status              From
 00    SRR6357   ● completed  42 s
 01    SRR6358   ◌ waiting           bbsplit[01] running
 02    SRR6359   ⊘ dropped           dropped in bbsplit
 03    SRR6360   ● running    12 s
```

**Chain view (new tab in the deck, chained maps only).** Items by stages for the whole chain, built from the snapshot alone:

```
            bbsplit  lint  subsample  strand  star
 00 SRR6357    ●      ●       ●         ●      ●
 01 SRR6358    ●      ●       ◐         ◌      ◌
 02 SRR6359    ✕      ⊘       ⊘         ⊘      ⊘     dropped in bbsplit
 03 SRR6360    ●      ●       ●         ●      ◐
 ● done  ◐ running  ◌ waiting  ✕ failed  ⊘ dropped
```

**Status wording.**
- An upstream-dropped clone shows "Dropped in <map>" everywhere: deck, `dropList`, header "Completed · N dropped". It is counted once, at its origin, in the header.
- A failed clone of a running map with `on_item_failure: drop` shows as dropped right away, unless the map itself ends `failed`.
- `droppedClones`, `effectiveStatus`, `cloneBucket` and `mapChip` gain these cases.

**Editor (`MapOverFields.tsx`).**
- A "Chain items" switch, enabled only when `over` comes from a map. Hint: "Start each item when <map> finishes it."
- A "Pair with" list for `zip`.
- Issues mirror V3 to V7.

Touched files: `plugins/taskKinds/horusMap/*`, `components/Canvas/ReactFlow/{MapCloneOverlay,MapCloneTable,cloneGridModel,DeletableEdge}.tsx`, `hooks/workflow/hierarchy.ts`, `components/Workflow/runFailure.ts`, `WorkflowHeader.tsx`, and the regenerated `client/types.gen.ts`.

Backend: no new table. The `tc-models` status enum follows the runtime; add `upstream_dropped`. Check `_settle_snapshot_tasks` and `_carry_cached_clones` treat it like `complete` for carried clones.

## Backward compatibility

- **Existing workflows:** unchanged; chaining is opt-in. The only behaviour change for every run is that maps stop taking an outer pool slot. More plain tasks may then run next to a map's clones; clones stay capped by the per-map `max_concurrency` and placement.
- **New YAML on an older pinned runtime** (release or campaign env):
  - pydantic ignores unknown fields, so `chain` is ignored and the run is still correct, only with a barrier;
  - `zip` is ignored, so `$reads` fails to substitute. That is loud, not wrong.
  - The backend should refuse to launch a workflow that uses `chain` or `zip` on a runtime older than 0.9.0. Check `get_horus_version` and the env pin in `run_launch`.
- **Old UI with a new snapshot:** `chain` and `zip` are ignored; `upstream_dropped` shows as skipped.
- **Released runtime with a new UI:** no `upstream_dropped`, and the chain view never appears.
- **Pins:** bump `horus-runtime>=0.9.0` in backend and orchestrator. Per memory, PluginEnvironments are never rebuilt, so check the prod env count before release.

## Test plan

Runtime unit tests (`tests/unit/workflow/test_map.py`, new `TestChain`, reusing `_split_task`, `_map_task`, `_wire`, `_FLAKY`, `_Node`):
- B[0] starts before A's last clone ends (gated bodies plus timestamps). A 3-map chain shows the same depth-first flow.
- Drops: A drops X, so B[X] and C[X] are `SKIPPED/upstream_dropped`, the slot gap holds, the B and C drop messages are right, and the map status is completed.
- `fail` policy: under `fail_fast` the run cancels with no leaked slots; under `continue`, B starts no new clones after A[X] fails and ends `upstream_failed`.
- `zip` pairing by slot, with and without `chain`. V1 to V9 load errors use the exact messages.
- Incremental: a second run skips everything; change one item and only its chain re-runs; when A is skipped whole, B runs the normal path; re-running after a drop retries only the dropped item down the chain.
- Capacity: with workflow `max_concurrency=1` and a chain of 4 maps, the run completes and the chain overlaps; placement capacity is respected (`test_lease.py` style).
- Lease: B on another `_Node`; A's machine is not released before B's last pull; a lost A[X] is recovered.
- Scheduler (`test_scheduler.py`): ready on stream open; cancel a blocked running waiter; wake-up without a completion.
- TUI snapshot with placeholders; `make babel-check`.

Frontend (`bun:test`): `hierarchy.test.ts` (chained edge flag, dropped propagation), `cloneGridModel.test.ts` (waiting and dropped-upstream buckets), `mapChip.test.ts`, `DroppedCount.test.ts`, a new `chainModel.test.ts` for the matrix, and `map.test.ts` for V3 to V7 issues.

Backend: an enum round-trip test for `upstream_dropped` in `backend/tests/crud/test_workflows.py`; carried clones.

E2E: a Playwright spec on the local E2E host stack (memory: Local E2E host stack). The workflow is split → A (chain head, `on_item_failure: drop`, one item fails) → B (`chain: true`, zip) → gather. Assert that:
- B's deck shows "waiting" while A runs;
- B[bad] shows "Dropped in A";
- the chain view renders;
- the header says "Completed · 1 dropped".

Also a `horus run` YAML smoke test in the runtime integration tests.

## Measuring the gain

1. In pantheon, add `w02-rnaseq/workflow.chained.yaml`, keeping the faithful port as the baseline:
   - `filter_trimmed`, `set_strandedness` and `filter_mapped` become per-item maps that drop a failing sample (open question Q3);
   - STAR, Salmon and the stats maps use `zip` instead of `*/$${id}` globs;
   - every per-sample map sets `chain: true`.
2. In the rig, add pipeline `rnaseq-chained` to `bench.py`. `compare.py` must still match the Nextflow reference.
3. Run `MODES="nextflow horus-cli horus-stack" PIPELINES="rnaseq rnaseq-chained rnaseq-tuned" ./matrix.sh 3` on the laptop and on c7i.4xlarge (`ec2/launch.sh` … `teardown.sh`).
4. Metrics:
   - median `wall_s`;
   - the gap from the first BBSplit clone done to the first STAR clone start, parsed from `horus.log`, or `stack/analyze.py` for stack runs;
   - CPU-hours, to check that overlap did not inflate work.
5. Expected: the critical path to the first STAR start drops from about 38 s to about 11 s (lint 2.0 + subsample 3.5 + salmon 4.3 + strand 0.6). Memory (15 GB per STAR or BBSplit clone on 31 GB) caps the overlap, so a realistic total gain is 10 to 25 s, which brings it roughly level with `rnaseq-tuned`. The report states the measured numbers, not this estimate.

## Delivery order

1. Runtime PR (horus-runtime): stream, fields, scheduler, validation, tests, i18n, and docs in `.claude/skills/horus-workflow/references/yaml-schema.md`. Tag v0.9.0 only after the user approves.
2. tc-os PR: pin bump, `tc-models` enum, frontend, launch guard, Playwright spec.
3. Pantheon PR: chained rnaseq port. Rig PR: `rnaseq-chained`. Bench report.

There are no merges without the user. Commits and PR titles have no emoji and no attribution.

## Decisions (user, 2026-11-03)

- Q1: opt-in `chain: true`.
- Q2: match by slot name within one item space.
- Q3: add `drop_exit_codes`.
- Q6: no map takes an outer pool slot, chained or not.
- Q4 and Q5 were not asked. The recommendation stands as the default: allow Python chains under the documented rule, and no depth priority in v1. Revisit after the bench.

`drop_exit_codes: list[int] = []` on horus_map:
- A clone failing with one of these exit codes is dropped (with the same propagation as `upstream_dropped`), whatever `on_item_failure` says. Any other failure follows `on_item_failure`.
- Validation: `"Map task '%(id)s' lists exit code %(code)d to drop items, but 0 means success."` for 0, and ints only in 1..255.
- The clone's `error` reads "Dropped: exit code N", and the UI shows "Dropped" instead of "Failed" for it.
- Tests: in `TestDropFailedItems`, a drop code under `on_item_failure: fail` drops the item, and a different code fails the map.
- The executor must expose the exit code on the failure. Check `ShellExecutor` and the docker/ssh executors (horus-docker, horus-ssh), and fix those in the plugins if missing.

## Open questions (with recommendation)

- **Q1 Declare or detect?** Recommendation: explicit `chain: true` on the downstream map. It keeps existing workflows exactly as they are, it lets the UI and validation know the intent without copying the runtime's body analysis, and an older runtime safely falls back to a barrier. Automatic detection could come later as an editor hint ("This map can start each item early").
- **Q2 Matching rule.** Recommendation: by slot name inside one item space, which is head-assigned and inherited, with gaps on drop. No user-defined keys in v1. Positional matching across different lists is rejected (V6).
- **Q3 Deliberate filters vs failures.** The port's sample filters are intentional drops, not errors; with `on_item_failure: drop` a real crash would also be dropped silently. Recommendation: add `drop_exit_codes: [N]` to horus_map. Only those exit codes drop; any other failure follows `on_item_failure`. The alternative is to use `drop` as it is and accept the looser semantics.
- **Q4 Python and other non-command bodies.** Recommendation: allow `chain: true` and trust the author (V8 documented), rather than refusing chains for Python maps.
- **Q5 Ordering under contention.** Recommendation: keep today's per-map pools and FIFO placement in v1, measure, and add depth-first priority (finish items already in flight) only if the bench shows downstream clones starving.
- **Q6 Outer pool slot for maps.** Recommendation: maps stop taking an outer `max_concurrency` slot for every workflow, not only chained ones, because a map already holds no machine slot. This is the only change that affects existing runs.
