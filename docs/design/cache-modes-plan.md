# Cache modes plan: input fingerprint cost on big folders + `cache: deep | fast`

Branch `cache-modes` off `map-chaining` 43fc77e (0.9.0 pre-release). Parent: bench coordinator (run run_6b5d264b3926); user decisions go through `orca orchestration ask`, not AskUserQuestion.

## Context

Measured 2026-11-05 on nf-core/rnaseq (12 samples x 15M reads, ParallelCluster + Slurm, FSx Lustre ~240 MB/s): after all tasks finished, Horus spent ~1 h hashing. 18 consumers each re-hashed the 30 GB markdup and 78 GB STAR map output folders on the head node, ~770 GB read at ~230 MiB/s. Nextflow's default cache key is path+size+mtime; content hashing is opt-in (`cache 'deep'`).

Where the cost comes from (at 43fc77e):
- `src/horus_runtime/core/artifact/store.py:67` `ArtifactStore.digest`: target-side `sha256sum` of every file of a folder, then sha256 of the sorted `<sha>  <relpath>` listing (marker line `horus-dir`).
- `src/horus_runtime/core/workflow/base.py:1519` `BaseWorkflow.source_digests`: digests each fingerprinted input at its source; skips reading only when `EdgeSource.digest` is set. Nothing is shared between tasks reading the same folder.
- `src/horus_builtin/workflow/map.py:901` a chained map re-digests whole streamed source folders after it settles, to write its manifest (`_write_manifest` at ~1340 reads `_streamed_digests`). This is the end-of-run hashing.
- `src/horus_builtin/workflow/scheduler.py:201` `input_digests = _Once(partial(workflow.source_digests, ...))` per task.
- `src/horus_builtin/task/horus_task.py:149` `_fingerprint` -> `TaskFingerprint(inputs, config_hash)`, manifest in `<wd>/.horus/<id>.json`; `map.py` `_fingerprint` keeps `_input_digests` for clones.
- Map layout: each clone's output is `root/<slot>` (`map.py:727`, `:1439`), so a map's output folder is one top-level entry per slot.

## Scope (user decisions)

1. Digest each artifact once per run: run-level cache keyed by (location, path [, mode]) used by `source_digests` and the map settle path. Invalidate when the producer reruns or is recovered.
2. Map folder digest composed from per-item digests: each clone digests its own slot on its own target right after it finishes; the map folder digest = hash of sorted (slot, item digest). Consumers and `map.py:901` use it without reading. Format change re-runs things once after upgrade (pre-release, documented in changelog/docs).
3. `cache: deep | fast`. deep = content sha256 (current, default). fast = relative path + size + mtime, no content read. Workflow-level default plus per-task override (Nextflow model). No per-input setting in v1.
4. Validation messages, i18n (`make babel-refresh`, Spanish entries; CI runs `babel-check`), docs in `.claude/skills/horus-workflow/references/yaml-schema.md`, tests.
5. tc-os editor control: design doc only, no tc-os implementation.

## Working design (to be validated by research/design waves)

- **Composable folder digest (Merkle).** Keep the single target-side command that lists `<sha>  <relpath>` (deep) or `<size> <mtime> <relpath>` (fast, via `find -printf`/`stat`, no read). Compose in Python: a directory digest = sha256 of sorted `(name, kind, child digest)`; a file's digest = its content sha256 (deep) or hash of size+mtime (fast). Then a map output folder digest = hash of sorted (slot, slot digest), equal to recomputing it from scratch, so a skipped map and a fresh consumer agree. Digest stays path-independent (source digest == staged copy digest).
- **Mode in the digest.** Prefix or salt fast digests (e.g. `fast:`) so deep and fast never collide; switching a task's mode re-runs it once.
- **Run cache.** On the workflow instance: `dict[(location_id, path, mode), asyncio.Future]`, so concurrent consumers share one in-flight digest. Producer output entries dropped when the producer task starts (covers rerun, retry on target loss, `_restage`, recovery). Pinned inputs and root inputs also cached (root inputs do not change within a run).
- **Per-item digests.** Clone digests its slot output on its own target right after `run_task(clone)` succeeds (data hot), stored on the map's `_ItemStream`/slot state; map digest composed in `_settle`; seeded into the run cache and `EdgeSource.digest` for consumers; `map.py:901` composes the streamed sources' digests from their streams' item digests instead of reading.
- **YAML surface.** `cache: deep | fast` on `horus_workflow` (default `deep`) and on tasks (default unset = inherit). Part of the task config hash? No: mode is captured by the digest prefix.
- **fast and transfers.** fast holds only if copies keep mtimes. tar keeps mtimes on extract; `LocalTarget.put_file/get_file_to` use `shutil.copyfile` (drops mtime). Audit scp/rsync/put_file paths in runtime and horus-ssh / horus-slurm / horus-docker; fix small gaps in plugins, report the rest. Note: inputs are digested at their source, so mtime loss mostly hits pinned/staged-digest paths; the codebase report must confirm.

## Team (team-of-agents, coordinator = this terminal, own Orca run)

All workers `--agent claude` in this worktree (docs waves) or own worktrees off `cache-modes` (implementation). Backend-only feature: no UX/frontend implementation roles; one UX/frontend designer for the tc-os editor control (doc only, read-only access to `~/Developer/tc-os`).

| Wave | Workers (parallel) | Output (docs/design/) |
|------|--------------------|------------------------|
| 0 Research | prior art (Nextflow cache modes incl. lenient, Snakemake rerun-triggers, Cromwell call-caching hashing strategies, Bazel, DVC, Toil/CWL, Make); community (pains with deep hashing, mtime on NFS/Lustre, copies dropping mtime); codebase (verify every file:line above, map folder layout, all digest callers, transfer mtime audit incl. plugins, risks) | `cache-modes-competitors.md`, `-community.md`, `-codebase.md` |
| 0b PM | one | `cache-modes-prd.md` |
| 1 Design | backend architect (runtime); UX/frontend designer (tc-os editor control, doc only) | `cache-modes-backend.md`, `cache-modes-ux.md` |
| 1b Tech lead | one, merges | `cache-modes.md`: WPs, contract, test plan, open questions |
| GATE | `orca orchestration ask` parent: PRD, design, YAML surface, default, migration effect, open questions. Wait. | decisions verbatim |
| 2 Implement | one per WP, disjoint write scopes (expected: core digest + run cache + mode field; map composed digest; plugin mtime fixes; docs/i18n last to avoid .po conflicts) | commits on WP branches, merged into `cache-modes` |
| 3 Review | correctness (`/code-review high`), performance (bytes read) | `review-*.md` |
| 3b Fix | one | commits |
| 4 E2E | only after parent says the local bench is done | report |

## Host constraints (put in every spec)

Until the parent says the bench is done: no Docker, no E2E, no stack runs, no parallel test runs; unit tests only, single process, `nice -n 19`. Never touch AWS. Never touch `~/orca/workspaces/tc-os/{bo-real,bo-local,bo-managed,bo-ec2,map-chaining}` or `~/orca/workspaces/pantheon/*`. No `pgrep -f`/`pkill -f` on paths. Signed commits, no attribution, no push/PR/tag, no bare `git stash`, never print remotes.

## Verification

- Unit (single process, `nice -n 19 uv run pytest -p no:xdist <files>`): once-per-run digest (N consumers -> 1 digest command per artifact); composed map digest equals recomputed `ArtifactStore.digest` of the map folder; deep vs fast invalidation (content change with same size+mtime: deep misses, fast hits; touch: fast misses); mtime kept across pack/unpack and local copies; benchmark-style test: big folder + N consumers, assert bytes read before (N x size) vs after (1 x size deep, 0 fast).
- `make lint type-check babel-check`.
- E2E only after bench is done (parent decides).
