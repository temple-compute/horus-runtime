# Cache modes: PRD

Product requirements for the `cache-modes` branch. Sources: `cache-modes-plan.md` (plan), `cache-modes-codebase.md` (codebase), `cache-modes-competitors.md` (competitors), `cache-modes-community.md` (community). Section references below point into those files.

## Problem

On 2026-11-05, nf-core/rnaseq (12 samples x 15M reads, ParallelCluster + Slurm, FSx Lustre at about 240 MB/s) finished all its tasks and then spent about 1 h hashing (plan, Context). 18 consumers each re-hashed the 30 GB markdup folder and the 78 GB STAR folder on the head node. That is about 770 GB read at about 230 MiB/s.

There are three causes (plan, Context; codebase §1):

- Every consumer digests its inputs at the source. Nothing is shared between tasks, except that map clones share the map's inputs (`base.py:1519`, `map.py:806`).
- A chained map re-reads whole streamed source folders after it settles so it can write its manifest (`map.py:901`). This produces the end-of-run tail.
- There is only one mode, a content sha256 of every file. Nextflow's default is path + size + mtime, and content hashing is opt-in (`cache 'deep'`) (competitors, Comparison).

The codebase report found two more full reads (codebase §1):

- On a stale re-run, `BaseTask.run` calls `is_complete()` with no digests, so the task hashes every staged input again on its own target (`task/base.py:452`, `horus_task.py:278-283`).
- Pinned inputs are hashed twice (`base.py:1543`, `horus_task.py:163-165`).

## Target users

- Workflow authors who run large data on HPC and shared filesystems: Slurm on Lustre or NFS, and nf-core ports. Every engine surveyed has a cheaper mode because these users asked for one (community §2).
- tc-os users who edit workflows in the tc-os editor. They need the same choice per workflow and per task. In this release that is a design doc only.
- Maintainers of tc-plugins and horus-ssh. They consume digests and copy files, so the format change and the mtime rules affect their code.

## User stories

1. As an author, I want each input folder hashed once per run however many tasks read it, so that hashing time does not grow with the number of consumers.
2. As an author, I want a map's output folder digest built from the per-item digests taken while the items ran, so that the run has no hashing tail after the last task.
3. As an author, I want to set `cache: fast` on a workflow and override it on one task, so that I skip content reads where I trust timestamps and keep `deep` where I do not.
4. As an author, I want an invalid `cache:` value rejected with a clear message in my language (English or Spanish).
5. As an author upgrading, I want to be told that tasks with folder inputs re-run once, and why.
6. As a tc-os user, I want a cache mode control on the workflow and on each task in the editor. This release only designs it.

## Success metrics

Measured on the nf-core/rnaseq case above: 18 consumers, 30 GB and 78 GB folders, about 770 GB read, about 1 h. The E2E rerun happens in wave 4, after the parent says the local bench is done.

| Metric | Today | Target, `deep` | Target, `fast` |
|---|---|---|---|
| Bytes read by digest commands for the two folders | ~770 GB | about 108 GB, each byte once, read by the clones while the data is hot | 0 for files of 1 MiB or more. Small files are read in full (see D2) |
| Head-node hashing after the last task | ~1 h | none: the map digest is composed without reading | none |
| Digest commands per artifact per run (unit test) | one per consumer | 1 | 1 (stat only) |
| Composed map digest compared with a digest recomputed from scratch | n/a | equal (unit test) | equal (unit test) |

A benchmark-style unit test builds a large folder read by N consumers. It asserts N x size bytes read today, 1 x size in `deep` and 0 in `fast` (plan, Verification). There is no byte-counter helper today. The test uses a target wrapper or a spy on the run cache (codebase §6).

## Decisions taken in this PRD (the user can override them)

- **D1. mtime precision for `fast`: whole seconds.**
  - Reason: GNU tar's default format, scp -p, SFTP v3 and Docker COPY all keep whole seconds only. With nanosecond precision, each of those copies would cause a re-run (community §4, §5; codebase §4).
  - Cost: two same-size writes within the same second become a silent false hit. This is inherent to `fast` and goes in the docs, as Nextflow documents it.
  - This also keeps the horus-ssh fixes to a single `preserve=True` each.
- **D2. `fast` hashes small files by content. The threshold is fixed at 1 MiB.**
  - Reason: files rewritten on every run (the loop index `loop.py:449`, the branch sentinel, re-materialized values) would otherwise re-run their consumers every time (codebase §4, §7.6).
  - Prior art: Snakemake uses the same 1 MB default (`--max-checksum-file-size`) (competitors, Comparison).
  - The threshold is not configurable in v1.
  - Side effect: a folder of many small files is still read in `fast` mode.
- **D3. Digest prefixes.**
  - Deep file digests stay plain sha256. They do not change, so file inputs do not re-run, and file digests already recorded by tc-os stay valid.
  - Deep folder digests become `deep:<hex>`, in the new Merkle format.
  - All fast digests become `fast:<hex>`.
  - A digest with no prefix for a folder is old-format. Any future format change takes a new prefix (for example `deep2:`).
  - Mode is not part of the task config hash: the prefix already captures it, so switching a task's mode re-runs that task once (plan, Working design; codebase §5).
- **D4. Subworkflow body `cache:`.** Today the expander drops it silently (codebase §5, §7.9). Proposal: the body's workflow-level `cache:` becomes the default for its inner tasks. Precedence: the inner task's own `cache:`, then the body's `cache:`, then the parent workflow's `cache:`.
- **D5. Where the format-change note goes.** horus-runtime and horus-docs have no CHANGELOG, and releases are GitHub releases (codebase §5). The note goes in:
  - the 0.9.0 GitHub release notes;
  - an "Upgrading" line in `yaml-schema.md`;
  - the horus-docs running-workflows page.

  No CHANGELOG file is created.
- **D6. Both double-hash fixes (stale re-run, pinned inputs) are in scope.** They are part of "digest each artifact once per run", and both are small: when `input_digests is None`, use the staged digests, and send the `_fingerprint` fallback through the run cache (codebase §2).

## Requirements

### Must

1. **Run-level digest cache.**
   - Key: `(location_id, generation, path_on_target, mode)`. The cache holds one in-flight future per key, so concurrent consumers share one digest.
   - Failures: `None` and exceptions are never cached.
   - Invalidation: drop entries when a task is dispatched, by path prefix in both directions. This covers clone re-runs, map retries, target-loss retries and in-place writers.
   - Reset: clear the cache at the end of the run.
   - It plugs into `source_digests`, the `_fingerprint` fallback and `is_complete(None)`. (codebase §2)
2. **Per-item digests and the composed map digest.**
   - Each clone digests its slot on its own target right after it succeeds.
   - The map digest is the Merkle composition of the sorted (slot, digest) pairs. Consumers and the manifest at `map.py:901` use it without reading.
   - It must equal a recompute from scratch, including the six edge cases in codebase §3:
     - fall back to a recompute when a slot removal failed;
     - prune empty directories;
     - give a symlinked slot the same result as the recompute;
     - read NUL-separated names and sort them as bytes;
     - re-digest a slot on re-run;
     - use the same mode on both sides.
   - Digests stay path-independent.
3. **`cache: deep | fast` field.**
   - On the workflow: default `deep`. On a task: unset means inherit.
   - Map clones and loop bodies inherit it through the existing copies (codebase §5).
   - The subworkflow body behaves as in D4.
4. **`fast` semantics: relative path, size and mtime in whole seconds, plus the content of files under 1 MiB.**
   - It walks every entry. It never relies on a directory's own stat (competitors, Avoid 2).
   - It needs a fallback for platforms without GNU `find -printf` (codebase §4, Portability).
5. **Prefixes as in D3.** A carried `EdgeSource.digest` is used only when it is in the consumer's mode (`base.py:1550`). Otherwise the consumer falls through to the cache.
6. **mtime kept on copies.**
   - `LocalTarget.put_file` and `get_file_to` keep mtime (`shutil.copy2`, or `copystat`) (`local.py:295`, `:317`).
   - horus-ssh SFTP `put` and `get`, and `scp`, use `preserve=True` (codebase §4). This is one argument each, so it falls under "small".
   - horus-slurm and horus-docker need no change.
7. **Validation.** An invalid `cache:` value raises a `WorkflowError` that lists the valid values. The message is written with `_()` in `src/horus_runtime` and has a Spanish entry. Strings under `horus_builtin` are never extracted (codebase §5, §7.10). `make babel-check` passes.
8. **Docs.**
   - `.claude/skills/horus-workflow/references/yaml-schema.md`: the top-level block, the task block and the skip section.
   - One line each in `references/execution.md` and `SKILL.md`.
   - The D5 upgrade note.
   - The `fast` caveats: same-second same-size writes, copies that drop mtime, and NFS attribute caching (community §4).
   - CI requires a linked horus-docs PR (codebase §5).
9. **Tests.**
   - Once-per-run digest: N consumers give 1 digest command.
   - The composed map digest equals the recomputed one.
   - Deep vs fast invalidation: a content change with the same size and mtime is caught by deep and missed by fast; `touch` makes fast re-run.
   - mtime is kept across pack/unpack and local copies.
   - The bytes-read benchmark test.
   - Reuse `_NoShellTarget` and the `ArtifactStore.digest` spy pattern (codebase §6).
10. **tc-os editor control: design doc only** (`cache-modes-ux.md`).

### Should

1. Seed the cache when an output is evacuated or relocated (`relocate_output`, `base.py:376`). Always in `deep`. In `fast`, only when the copy kept mtimes (codebase §2, §4b).
2. Log the effective cache mode for each task at debug level, to help diagnose misses.

### Won't (v1)

- A per-input mode, and the `lenient` and `off` modes.
- A persistent cache that survives across runs and is keyed on file stat data, as in DVC and Bazel (competitors, Borrow 7).
- A printed reason for each cache miss. Users ask for it (community §1.4), so it is a follow-up candidate.
- `tar --format=posix`. D1 makes it unnecessary.
- The package hash on every unpack (`folder.py:141`).
- tc-plugins `output_digests` re-hashing map folders.
- The horus-ssh perf-branch check that digests both ends of a local-to-SSH copy (codebase §7.8).
- Any tc-os or tc-plugins code, including the fix needed for Risk 1.

## Non-goals

- Changing the file digest format or the task config hash.
- Making `fast` safe against same-second rewrites, touch, or mtime restored on purpose. Those are documented, not prevented.
- Making `fast` the default. Every tool surveyed keeps it opt-in or uses metadata with documented caveats, and the main complaint is false misses (community §5).

## Risks

Ranked as in codebase §7.

1. **tc-os recorded digests after the upgrade (high).**
   - tc-plugins stores old-format folder digests in its S3 manifests.
   - After the upgrade, a lazily skipped producer carries such a digest. `CachedOutputTransfer` compares it with the new format and **fails the consumer** (`s3_transfer.py:559-565`).
   - D3 makes old folder digests recognisable. tc-plugins must still treat a folder digest with no prefix as missing.
   - tc-os vendors the runtime (`~/Developer/tc-os/horus-runtime`). The bump there must ship together with that tc-plugins change.
2. **`fast` mode with tc-os S3 caching.**
   - Recorded and carried digests are deep, so a fast consumer must ignore them (Must 5).
   - `S3CacheTarget` has no shell. A fast consumer of a lazily skipped output therefore re-runs every time, unless tc-os also records fast digests.
   - S3 single-file restores get a new mtime.
   - The `CachedOutputTransfer` check must stay deep.
3. **A stale run cache** gives a wrong skip if invalidation misses a writer. Mitigations:
   - invalidate on dispatch by path prefix in both directions;
   - put the generation in the key;
   - never cache a failure.
4. **The composed map digest differs from the recomputed one.** The only effect is extra re-runs, never wrong results. The tests cover the codebase §3 cases.
5. **`fast` precision and portability.**
   - Lustre and NFS granularity, and NFS attribute caching seen from the head node, cause false misses.
   - A same-second, same-size write causes a false hit.
   - `find -printf` is GNU only.
6. **Generated files rewritten on every run.** D2 mitigates this.
7. **Upgrade cost.** Every task with a folder input re-runs once. This is acceptable in a pre-release and is documented through D5.
8. **The subworkflow `cache:`.** It is dropped silently unless D4 is implemented.
9. **i18n.** Messages under `horus_builtin` are not extracted, so new user-facing messages must live in `src/horus_runtime`.

## Open questions for the user

1. D1: whole-second mtime for `fast`. Do you accept the wider window for same-second false hits?
2. D2: content hash for small files at a fixed 1 MiB, even though a folder of many small files is still read. Should the threshold be configurable now, or later?
3. D3: deep file digests stay unprefixed, so file inputs do not re-run. Or should every digest be prefixed, which is more uniform but re-runs file inputs once too?
4. D4: subworkflow precedence (inner task, then body, then parent). Should the subworkflow task's own `cache:` in the parent also apply to the body?
5. D5: release notes plus docs, with no CHANGELOG file. Or create `CHANGELOG.md` now?
6. Risk 1: who changes tc-plugins to treat folder digests with no prefix as missing, and when? Until then, tc-os must not bump its vendored runtime.
7. Risk 2 and codebase §7 (end): tc-os may download root inputs fresh into each run. If it does, `fast` re-runs everything there. Should `fast` be documented as not useful in tc-os until tc-os records fast digests and keeps the mtimes of root inputs?
