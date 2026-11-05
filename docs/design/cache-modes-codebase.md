# Cache modes: codebase report

Wave 0 codebase research for `docs/design/cache-modes-plan.md`. HEAD is `e6df3da` on `cache-modes` (code is the same as `43fc77e`). Everything here comes from reading the code. Nothing was run. Plugin sources were read at:

- horus-ssh: `~/Developer/horus-ssh-perf` at 8a8c339 (branch perf/task-launch), with `origin/main` (v0.0.9) for comparison.
- horus-slurm: `~/Developer/horus-slurm` at 676c12e.
- horus-docker: `~/Developer/horus-docker` at 19569d8 and `~/orca/workspaces/horus-docker/bench-official`.
- tc-plugins: `~/Developer/tc-os/tc-plugins` on main at b712a32c. Read only, because tc-plugins is the main digest consumer outside the runtime.

## 1. Plan claims checked against HEAD

Correct:

- `store.py:67` `ArtifactStore.digest`. For a file it returns the content sha256. For a folder it runs `find . -type f -print0 | sort -z | xargs -0 sha256sum` on the target, then takes the sha256 of the whole output, which starts with the `horus-dir` line (`store.py:86-97`).
- `base.py:1519` `source_digests`. It reads the source unless `EdgeSource.digest` is set (`:1550`). A source that raises `NotImplementedError` (storage-only) gives `None` (`:1560-1563`).
- `map.py:901` (the call starts there) re-digests the streamed sources. `_write_manifest` is at `map.py:1340` and reads `_streamed_digests` at `:1346`.
- `scheduler.py:201` creates `_Once` once per task attempt. It is created again on a target-loss retry (`scheduler.py:321`).
- `horus_task.py:149` `_fingerprint`. The manifest path `<wd>/.horus/<id>.json` is built at `horus_task.py:137-147`. Map `_fingerprint` at `map.py:1324` keeps `_input_digests`. Clones receive them at `map.py:806` (`EdgeSource.digest`) and `:1487`.
- Map layout: the slot dir is created at `map.py:727-729` and set on the clone's output at `:1439-1440`.
- `LocalTarget.put_file/get_file_to` use `shutil.copyfile` (`local.py:295`, `:317`), which drops mtime.

Wrong or incomplete:

- "Nothing is shared between tasks" is mostly right. The exception is map clones, which reuse the map's shared-input digests (`map.py:806`, `:1487`).
- **The plan names only part of who sets `EdgeSource.digest`.** In the runtime only `map.py:806` sets it. The "cached outputs in storage" case is tc-plugins: `artifact_cache.py:578-586` relocates lazily skipped outputs to an `S3CacheTarget` that carries the recorded digest.
- "tar keeps mtimes on extract" is true, but GNU tar 1.35 defaults to `--format=gnu`, which stores whole seconds. A tar round trip therefore truncates sub-second mtimes.
- The plan says producer entries are dropped "when the producer task starts (covers ... `_restage`, recovery)". `_restage` and recovery do not re-run a producer. They move data to a new machine generation or to a new location. The cache key must include the generation. A relocation then naturally gets a new key, and it can be seeded with the producer's digest.
- Digest readers the plan does not list:
  - `BaseTask.run` calls `is_complete()` with no digests (`task/base.py:452`). On a re-run where old outputs and the manifest still exist, `HorusTask.is_complete` (`horus_task.py:278-283`) then calls `_fingerprint(None)`, which re-hashes **every staged input** on the task's target, after the scheduler has already hashed them at the source. This is a second full read on every stale re-run.
  - Pinned inputs are hashed twice. `source_digests` hashes them at `base.py:1543-1547`. Then `_transfer_and_run` leaves pinned inputs out of `_staged_digests` (`scheduler.py:420-426`), so `_fingerprint` hashes them again (`horus_task.py:163-165`).
  - `FolderArtifact.unpack_command` hashes the whole package on every unpack (`folder.py:140-141`). This is a read of the compressed bytes, and only happens on cross-location transfers.
  - In horus-ssh perf/task-launch only (not v0.0.9 main), `LocalToSSHTransfer` digests both the source and the destination before every copy (`ssh_transfer.py:216-219`).
  - In tc-plugins:
    - `output_digests` digests every output of every task after it runs (`side_product_upload.py:449-473`, started at `:651`). Map clones are excluded, but the map task's own folder is not.
    - `CachedOutputTransfer` re-digests every pulled cached output and **raises** if it does not match the recorded digest (`s3_transfer.py:533`, `:545`, `:551-565`).

## 2. Digest producers and consumers, and where a run cache plugs in

Proposed cache: `dict[(location_id, generation, path_on_target, mode), Future[str | None]]` on the workflow, as a PrivateAttr next to `_relocations` (`base.py:332`).

- **Key.** It needs the generation as well as the location. A replaced releasable machine keeps its `location_id` but loses its data. tc-plugins already keys its pull set this way (`s3_transfer.py:524-529`), and so does the in-flight transfer table (`strategy.py:126-133`). Use the resolved `path_on_target`, not `artifact.path`: SSH maps paths (`ssh_target.py:~433-480`).
- **Failures.** Never cache `None` or an exception. A digest that dies with a `TargetLostError` must be retried.
- **Reset.** Clear it at the end of the run, where `LeaseManager` clears relocations (`lease.py:925`).

Where it plugs in:

| Reader | Location | Plug-in |
|---|---|---|
| `source_digests` (pinned branch and source branch) | `base.py:1543`, `:1557` | Replace both `ArtifactStore(...).digest` calls with `workflow.digest(target, artifact, mode)`. This one change covers the task-skip middleware, the bound-task middleware, `is_complete_before_transfer`, `manifest_matches` and `map.py:901`, because all of them go through `_Once` and `source_digests`. |
| `EdgeSource.digest` short-circuit | `base.py:1550` | Use it only when it is in the consumer's mode (see the risks). Otherwise fall through to the cache. |
| `HorusTask._fingerprint` fallback (staged copies, pinned inputs) | `horus_task.py:163-165` | Use the cache, keyed on the task's own target. |
| `HorusTask.is_complete(None)` from `BaseTask.run` | `horus_task.py:278-283`, `task/base.py:452` | Smallest fix: when `input_digests is None`, use `self._staged_digests`. This removes the second full read on stale re-runs. |
| Map clones | `map.py:800-809`, `:1487` | Already shares the digests. The clone's item (`digest=None` today) could take its digest from the collection's Merkle tree, so the collection is not read twice. |
| Chained per-item sources | `map.py:793-799` | Can carry the producer clone's slot digest (`digest=`), so a chained clone does not re-read its item. |
| Map streamed sources | `map.py:901` | Compose them from the source stream's per-slot digests (stored on `_ItemStream`, `map.py:105`) instead of reading. |
| tc-plugins `output_digests` | `side_product_upload.py:449` | Can seed the cache (same location and path), and so save every consumer's read. tc-os scope only. |
| `LocalToSSHTransfer` | `ssh_transfer.py:216` | The source side can use the cache. This check must stay deep (see section 4). |

When cache entries become invalid:

- **Producer re-runs, including a target-loss retry, a map retry, clone re-runs, and in-place writers.** Drop the entries for every output path of the task when it is dispatched (`scheduler.py:434`, just before `target.dispatch`), on that location. Drop by path **prefix in both directions**: entries under the output and entries above it. A clone writing `root/<slot>` invalidates `root`, and a task writing into another task's folder (a `transfer=False` edge, or an output path equal to an input path, see `artifact_cache.py:457-490`) invalidates that folder. Clones, early re-runs (`map.py:1088-1111`) and `_rerun_lost` (`:1137`) all dispatch through `execute_task`, so this one hook covers them.
- **Map folder.**
  - Stale slots are removed at the start (`map.py:743-745`) and dropped slots at settle (`:1271`). Seed the composed map digest only after `_settle` (`map.py:886`), and invalidate `root` whenever a slot is re-run (`_take_rerun`, `map.py:1113`).
  - A per-slot digest belongs to the `(slot, generation)` that produced it. Clear it in `_take_rerun` and when `_restage` runs (`map.py:1035`).
- **`_restage` and target loss.** The new generation changes the key, so no explicit drop is needed. `leases.forget(clone)` (`map.py:733-735`) is not a cache signal.
- **Recovery and evacuation.** `relocate_output` (`base.py:376`) points consumers at a new location and path. Evacuation copies verbatim, so the producer digest can be seeded under the new key in deep mode. In fast mode it can be seeded only if the copy keeps mtimes (section 4). tc-plugins already carries the digest on S3 relocations.
- **Loops.** Every iteration gets a unique id and unique output paths (`loop.py:408`, `:424-426`). The index file is iteration-unique as well (`:448-452`). A path key is safe there.
- **Subworkflows.** Inner tasks are prefixed and run once (`expander.py:255-300`). Nothing special is needed.
- **Root inputs.** Cache them for the whole run. A user editing a root input during a run gets the first digest, which is acceptable.

## 3. Map output layout

- Each clone's output is `<map output>/<slot>` (`map.py:727`, `:1439`). The map creates it as a **directory** before the clone runs (`:728`).
- The map output must be a folder (`map.py:584-602`). Nothing stops a body from replacing its slot with a file or a symlink, so a slot is normally a folder but can be either.
- Slot names are zero-padded indices, or the upstream slot names when the map iterates over another map's output (`map.py:656-671`).
- Nothing else lands in the map output folder in normal use:
  - Clone manifests go to `<target wd>/.horus/<map>[<slot>].json` (clone target = map target `idle_copy`, `map.py:1463-1470`).
  - Items from another machine go to `<wd>/.horus_items/<task>` (`map.py:781-786`).
  - Unpack markers `.<name>.horus-sha256` and the `.horus-tmp$$`/`.horus-old$$` siblings sit in the destination's parent (`folder.py:131-134`).
  - Side products go to `<wd>/<task id>/side-artifacts` (`task/base.py:402`).
- Edge: a map whose output path is `.` is not rejected. Its root would then be the working directory, and the stale-slot sweep (`map.py:743-745`) would delete siblings.

**Composition.** Proposed Merkle digest: a directory is H(sorted (name, kind, child digest)), and a file is the sha256 of its content. Composing the map digest from per-slot digests equals recomputing it over the folder from scratch when:

- the root holds exactly the slots;
- the per-slot digests use the same rules as the recompute;
- each slot digest was taken after the slot's last write.

Cases where the two would differ:

1. `_remove_slots` only logs a slot it could not remove (`map.py:203-237`). The recompute then includes that slot and the composition does not. Fall back to recomputing, or fail, when the removal fails.
2. Empty directories. Today they are invisible (`find -type f`). The Merkle form must prune subtrees with no regular file, or an empty slot would count in one form and not the other.
3. A slot that is a symlink. The recompute from the root skips it (`find -type f` without `-L`). A per-slot `cd slot && find .` follows it.
4. Name handling. The listing today is newline-separated, and `sha256sum` escapes odd names with a leading `\`. The new format must read NUL-separated output and sort raw bytes (`LC_ALL=C`), not Python `str`.
5. A slot changed after it was digested: an in-place write by a chained consumer on the same machine, or a lost and re-run slot. Re-digest it on re-run and drop it on loss.
6. The mode. Both sides must use the same mode.

Files keep their plain sha256. Only folder digests change format, so only tasks with folder inputs re-run once after the upgrade.

## 4. Transfer mtime audit

| Path | Copies with | mtime kept? | Smallest fix |
|---|---|---|---|
| `FolderArtifact.pack_command` (`folder.py:107-111`) | `tar --use-compress-program='gzip -1' -cf` / `tar czf` | Whole seconds only (GNU default format `gnu`) | Use whole-second mtimes in fast digests. Alternatively use `--format=posix`, but busybox and bsdtar differ. |
| `FolderArtifact.unpack_command` (`folder.py:139-151`) | `tar xzf` into a temp dir, then `mv`. Skipped when the package sha matches the marker. | Yes (tar restores member mtimes, no `--touch`) | None. When the marker matches, the old copy is kept with its old mtimes, which is stable. |
| `GenericTransfer` (`generic.py:77-103`) | package, then `get_file_to`, `put_file`, then unpack. A single file is its own package and is moved into place with `mv` (`store.py:150-157`). | Folder: as tar. **Single file: no.** | Keep mtime in `get_file_to`/`put_file` (below). |
| `LocalTarget.put_file/get_file_to` (`local.py:295`, `:317`) | `shutil.copyfile` | No | `shutil.copy2`, or `copyfile` plus `copystat`. Only the `Path` branch, not bytes. |
| `BaseTarget.get_file_to` default (`target/base.py:616-628`) | `write_bytes(get_file())` | No | Leave it. A target without streaming has no remote mtime to keep. |
| `FolderArtifact.write` (`folder.py:88`) | `shutil.copytree` (copy2) | Yes | None |
| horus-ssh `put_file` (`ssh_target.py:739`, `sftp.put` at `:766`; main `:724`) | asyncssh SFTP, `preserve=False` by default | No | `preserve=True` |
| horus-ssh `get_file_to` (`ssh_target.py:817`, `sftp.get` at `:839`; main `:797`) | asyncssh SFTP | No | `preserve=True` |
| horus-ssh `SSHToSSHTransfer` (`ssh_transfer.py:176`; main `:172`) | `asyncssh.scp(..., recurse=is_dir)` | No, for files and folders | `preserve=True` |
| horus-ssh `LocalToSSHTransfer` (perf branch, `ssh_transfer.py:216-223`) | digest compare, then `GenericTransfer` | n/a | Keep this check deep. Fast mode would compare the source mtime with the copy's mtime. |
| horus-slurm (`slurm.py:476-492`) | delegates to `inner` (SSH or local). `location_id` is the inner one (`:431-438`), so a shared filesystem means no copy at all. | Same as inner | None of its own |
| horus-docker (`executor/docker.py:259-281`) | bind mounts. `put_file` is used only for Dockerfile bytes (`:356`). | No copy | None |
| tc-plugins `_copy_old_dir` (`s3_transfer.py:604-607`) | `cp -a` | Yes | None |
| tc-plugins S3 single-file restore (`s3_transfer.py:277-325`) | curl / httpx download, then `put_file` | No | Out of scope (tc-os). See the risks. |
| tc-plugins S3 folder restore (`s3_transfer.py:~348-370`) | tarball, then `ArtifactStore.unpackage` | Whole seconds | As tar |
| Generated files: loop index (`loop.py:449`), branch sentinel (`branch.py:336`), value materialize (`value.py:61-71`, only when the value changed) | `put_file` / `write` | New mtime on every rewrite | The loop index is written every run, so its consumer re-runs every time in fast mode. Hash the content of small files (for example under 1 MiB) even in fast mode. |

**Where a fast digest of the source differs from the staged copy.**

- Single files sent through `GenericTransfer` (local and SSH), all of `SSHToSSHTransfer`, and S3 single-file restores give a new mtime.
- Any tar round trip loses the sub-second part, unless fast digests use whole seconds.
- Shared filesystems (Slurm, local to local) and Docker do not copy, so the digests are equal.

**Does it matter for stability across runs?** Mostly no:

- Fingerprints use source digests (`base.py:1519`). The staged copy is digested only in a few places:
  - pinned inputs;
  - the `_fingerprint` fallback;
  - the stale-re-run check (`task/base.py:452`), which only decides "run". A mismatch there costs time, never correctness.
  - a map clone's item, which is digested on the map's staged copy (`map.py:800-809`).
- The clone item is stable from run to run, because the unpack marker keeps the copy, or tar restores the same whole-second mtimes. It is not stable when the collection is single-file items sent by SFTP (each item is a child of a folder transfer, so this does not happen today).

It does matter in three places:

- **(a) tc-plugins `CachedOutputTransfer`.** It checks the pulled copy against the recorded digest and fails the consumer on a mismatch (`s3_transfer.py:559-565`). This check must stay deep.
- **(b) Evacuated or relocated sources.** A consumer that digests the evacuated copy gets a different fast digest than one that digested the producer's original. That causes spurious re-runs that depend on timing. Seed the cache, or carry `EdgeSource.digest`, at evacuation.
- **(c) The perf-branch `LocalToSSHTransfer` check.** With full-precision mtimes it never matches, which is safe but always copies. With whole seconds it matches when fast would.

**Portability.** `find -printf` is GNU only, and `stat` flags differ between GNU and BSD. The fast command needs a fallback in the style of the existing `shasum` fallback (`store.py:87`).

## 5. Where `cache: deep | fast` goes

- **Workflow.** `BaseWorkflow`, next to `failure_policy` (`base.py:272`): `cache: Literal["deep", "fast"] = "deep"`. `HorusWorkflow` (`horus_workflow.py:36`) adds no fields of its own.
- **Task.** `BaseTask`, next to `skip_if_complete` (`task/base.py:269`): `cache: Literal["deep", "fast"] | None = None`. The effective mode is `task.cache or workflow.cache`.
- **Clones and copies.**
  - Map clones pick up the field automatically through `model_dump` (`map.py:1446-1470`).
  - Loop bodies pick it up through `body_template` (`loop.py:405-410`).
  - **A subworkflow body's workflow-level `cache:` would be dropped silently.** The expander takes only the body's tasks and edges (`expander.py:118`, `:230-300`). Choose: inherit the parent's mode, or apply the body's mode to its inner tasks.
- **Config hash.** Nothing to do. The hash covers only runtime, executor, code and `_config_extra` (`horus_task.py:170-182`), so a new `BaseTask` field stays out of it. The pinned-hash tests `test_map.py:2071` and `test_builtin_task.py:~585-595` keep passing.
- **Validation.** A `Literal` gets pydantic's English error, as `failure_policy` and `on_item_failure` (`map.py:286`) do today. No translated `Literal` errors exist. Only add a `field_validator` that raises a `WorkflowError` with `_()` if a custom message is wanted.
- **i18n.**
  - `babel.cfg` is `[python: **.py]`, but the Makefile extracts from `SOURCE_DIR = src/horus_runtime` only (`Makefile:13-17`, `:83-87`). **Strings in `horus_builtin` (map.py, horus_task.py, scheduler.py) are never extracted.** Of the code touched by this feature, only new `_()` strings in `src/horus_runtime` (store.py, workflow/base.py, task/base.py) need Spanish entries.
  - The Spanish file is `src/horus_runtime/locale/es/LC_MESSAGES/horus_runtime.po`. `babel-refresh` = extract + update (`Makefile:92`).
  - CI runs `babel-check` (`python-ci.yaml:40`, `release.yaml`).
- **Docs.**
  - `.claude/skills/horus-workflow/references/yaml-schema.md`: the top-level block (`:9-35`), the task block next to `skip_if_complete` (`:45`) and the skip section (`:250`).
  - `references/execution.md:51-70` and `SKILL.md:34-36` describe the fingerprint and need a line each.
  - CI requires a linked horus-docs PR (`docs-check.yml`). Relevant pages: `horus-docs/content/docs/guides/running-workflows.mdx`, `sdk/core/task.mdx` and `sdk/core/workflow.mdx`.
- **CHANGELOG.** There is none in horus-runtime or horus-docs. Releases are GitHub releases (`release.yaml` fires on `release: published`). The format-change note has to go in the release notes and the docs, or a CHANGELOG has to be created (the PRD should decide).
- **JSON schema.** No checked-in files. Schemas are built at runtime by `AutoRegistry` (`auto_registry.py:344`), so the field appears automatically. The tc-os editor reads it from there.
- **tc-os vendoring.** tc-os carries a copy of the runtime at `~/Developer/tc-os/horus-runtime`.

## 6. Tests to update and helpers to reuse

To update or extend:

- `tests/unit/artifact/test_store.py:140-170`: folder digest semantics.
- `tests/unit/workflow/test_edges.py:787-840` (`TestSourceDigests`): it uses `_NoShellTarget` (`:765-778`), a target that fails the test on any command. Reuse it to prove "no read".
- `tests/unit/workflow/test_scheduler.py:1615-1720`: shared digests. `:1717` pins a file digest as plain sha256, which still holds.
- `tests/unit/workflow/test_map.py:517-590`: the map hashes shared inputs once (a `patch.object(ArtifactStore, "digest", spy)` counter, which is the pattern to reuse for "N consumers, 1 digest").
- `tests/unit/workflow/test_map.py:3956-4000`: the chained map manifest equals a fresh `ArtifactStore.digest` of the folder. This is the place for the "composed == recomputed" test.
- `tests/unit/workflow/test_map.py:2071`: config hash unchanged.
- `tests/unit/workflow/test_map.py:2158`: partial run removes the manifest.
- `tests/unit/task/test_builtin_task.py:447-600`: manifest and fingerprint.

Counting commands and bytes:

- `test_map.py:2245-2305` wraps `LocalTarget.run_command_sync` with `patch.object(..., side_effect=LocalTarget.run_command_sync)`. Filter the calls for `sha256sum` or `find` to count digest commands.
- There is no byte-counter helper. A wrapper target that sums file sizes per digest command, or a spy on the cache, is enough.

Fake targets:

- `tests/unit/transfer/test_transfer.py:~40-110`: a full in-memory target.
- `tests/unit/middleware/test_middleware.py:141`, `tests/unit/artifact/test_iterable.py:78`.
- `tests/unit/core/test_lease.py:242-255`: a fake evacuator and recovery.
- `tests/unit/workflow/test_map.py:2525`: map recovery.
- `tests/conftest.py`: `make_shell_task`, `horus_context`.

tc-plugins tests that patch `ArtifactStore.digest`: `test_s3_transfer.py:722-900`, `test_side_product_upload.py:478-565`, `test_artifact_cache.py`.

## 7. Risks, highest first

1. **The folder-digest format change against digests tc-os has already recorded.** tc-plugins stores `tc_output_digests` in S3 manifests (`side_product_upload.py:413`, `artifact_cache.py:377-445`). After the upgrade, a lazily skipped producer relocates its output with the old-format digest, and a consumer that runs pulls it. `CachedOutputTransfer` then compares a new-format digest with the old one and **fails the consumer** with "re-run task with cache off" (`s3_transfer.py:559-565`). Version the digest (for example `deep:`/`fast:` prefixes, which the plan wants anyway) and make tc-plugins treat recorded digests without a current prefix as missing.
2. **Fast mode with tc-os caching.**
   - Recorded and carried digests are deep. A fast consumer must not use a carried deep digest (`base.py:1550`), or its fingerprint changes depending on whether the producer was lazily skipped.
   - `S3CacheTarget` has no shell (`NotImplementedError` gives `None`, `base.py:1560`), so a fast consumer of a lazily skipped output always re-runs unless fast digests are recorded too.
   - S3 single-file restores get a new mtime.
   - The verification in `CachedOutputTransfer` must stay deep.
3. **A stale run cache.**
   - Invalidation must happen on every dispatch, by path prefix in both directions, and the key must include the generation and the on-target path.
   - The cases to cover are clone re-runs, map retries, in-place writers (`transfer=False` edges, an output equal to an input) and nested output paths.
   - A cached `None` or exception (a target lost mid-digest) must not stick.
4. **Composed map digest ≠ recomputed** (section 3): a slot whose removal failed, the rule for empty dirs, a symlinked slot, name escaping and sort order. A mismatch makes a skipped map and a fresh consumer disagree, so the consumer re-runs. Results stay correct.
5. **Fast-mode portability and precision.** `find -printf` is GNU only. The tar format keeps whole seconds. Lustre and NFS timestamp granularity and clock skew between nodes can cause misses. Two writes within the same second with the same size are a silent false hit. That is inherent to fast mode and must be documented, as Nextflow does.
6. **Generated files rewritten every run** (loop index `loop.py:449`, branch sentinel, a re-materialized value) make fast mode re-run their consumers every time. Hash small files by content.
7. **Upgrade cost.** Every task with a folder input re-runs once (file digests do not change). This is expected for a pre-release, and has to be stated in the release notes because there is no CHANGELOG.
8. **Performance holes the run cache alone does not close:**
   - the double hash on stale re-runs (`task/base.py:452`);
   - the double hash of pinned inputs;
   - the package hash on every unpack (`folder.py:141`);
   - tc-plugins hashing every output after each task, including whole map folders (`side_product_upload.py:449`);
   - perf-branch horus-ssh hashing both ends of each local-to-SSH copy.
9. **The subworkflow body's `cache:` is dropped** unless the expander handles it (`expander.py:118`).
10. **i18n trap.** Messages added under `horus_builtin` are never extracted (`Makefile:17`). This is consistent with today, but the plan's "Spanish entries" step only applies to `src/horus_runtime` strings.

Open question for tc-os: are root inputs downloaded fresh into each run's directory? If so, their mtimes change every run and fast mode re-runs everything there.
