# Cache modes: correctness review

Scope: `git diff cb82b22..HEAD` (2e37142, ae7a20e, 126ecd4, 45cbaa8, 757cc73) against `docs/design/cache-modes.md`, plus horus-ssh `a0b78a1`. I only read the code. I ran no tests. Line numbers are at HEAD (`757cc73`) and horus-ssh `a0b78a1`.

## Findings, ranked

### H1. horus-ssh `preserve=True` also copies permissions, so a later copy over a read-only file fails (coordinator item c: real)

- Where: `horus-ssh src/horus_ssh/target/ssh_target.py:725` (`sftp.put`), `:803` (`sftp.get`), `src/horus_ssh/transfer/ssh_transfer.py:176` (`asyncssh.scp`).
- Problem: in asyncssh, `SFTPClient._copy` with `preserve` calls `setstat(dst, SFTPAttrs(permissions=..., atime, mtime))` (`asyncssh/sftp.py:4013-4019`). The copy gets the source's mode. A later SFTP open of the same destination with `"wb"` then fails with permission denied, because the runtime does not run as root.
- Failure scenarios (persistent working directory, which `skip_if_complete` needs anyway):
  - A `PythonScriptRuntime` script that is 0444 locally (read-only checkout, Nix store). `python_script.py:107` calls `put_file(script, <wd>/<task>/script.py)` on every run. Run 1 leaves a 0444 remote file. Run 2 re-runs the task, the put raises `SFTPPermissionDenied`, and the task fails.
  - A read-only side artifact on the node. `executor/base.py:297` calls `get_file_to` to the same local landing path. Run 2 raises `Could not write ...`.
  - SSH to SSH through `GenericTransfer`: `get_file_to` keeps 0444 in the temp file, then `put_file` keeps 0444 at the destination. The next transfer to that path fails.
- scp (plausible): the OpenSSH sink already creates files with the source mode, minus umask, so the file side of this is not new. `-p` adds exact directory modes: a 0555 source directory gives a 0555 copy, and a later `rm -rf` or re-copy into it fails.
- Minimal fix: keep times only, matching LocalTarget's `_copy_keeping_mtime`.
  - put: drop `preserve`. After the put, `st = content.stat()`, then `await sftp.utime(remote_path, times=(st.st_atime, st.st_mtime))`.
  - get: drop `preserve`. `a = await sftp.stat(remote_path)`, then `os.utime(local_path, (a.atime, a.mtime))`.
  - Make both best-effort: catch `SFTPError`/`OSError`, because a lost mtime only costs a false miss.
  - scp: leave `preserve=True`, or follow it with `chmod -R u+w` on the destination.

### H2. Run-cache invalidation runs only at dispatch start, so a digest taken while the writer runs stays cached after it finishes (wrong skip)

- Where: `src/horus_builtin/workflow/scheduler.py:432` (the only invalidation for task writes).
- Problem: `forget_digests` runs before `target.dispatch`. Any reader that digests a path at, under or above the task's outputs while the task runs caches the pre-write or mid-write state. That entry survives the task's completion. Consumers ordered after the writer then hit it. Before ae7a20e every consumer re-read the path.
- Concrete scenario (nested outputs):
  - W1 outputs `results/` and W2 outputs `results/b`. C1 depends on W1 and reads `results/`. C2 depends on W1 and W2 and reads `results/`.
  - Run 2: W1 skips and W2 re-runs because its input changed.
  - C1 digests `results/` after W2 was dispatched but before W2 wrote anything. That digest equals last run's final state.
  - C2 runs after W2, hits that cached digest, matches its manifest and SKIPS, even though `results/b` changed.
  - The same holds for any reader of a folder above or under a running writer's output.
- Minimal fix: forget again when the task ends. In the `finally` at `scheduler.py:448`, add `workflow.forget_digests(target, [target.path_on_target(o) for o in task.outputs])` next to `task._staged_digests = None`. This also drops an entry still in flight: its owner never re-inserts (identity check at `base.py:476`), so the fix is complete.

### M1. `cache:` next to the `sub:` shorthand is dropped silently (coordinator item b: real)

- Where: `src/horus_builtin/workflow/subworkflow/lowering.py:57-68` builds a fresh dict with only `kind/id/name/description/body/port_overrides/max_depth/target`. The long `kind: subworkflow` form keeps `cache` (`expander.py:306-311`).
- Failure scenario: a workflow with `cache: fast` and a subworkflow written as `{id: qc, cache: deep, sub: {...}}`. The inner tasks run in `fast`, a less safe mode than the user asked for (same-size same-second false hits). The yaml-schema doc says it is dropped, but nothing warns.
- Minimal fix: `if entry.get("cache") is not None: data["cache"] = entry["cache"]`, then delete the "A `cache:` next to `sub:` is dropped" sentence in `references/yaml-schema.md`. `skip_if_complete` is dropped the same way, which is pre-existing.

### L1. `HorusTask._fingerprint` reads `self.workflow` eagerly and raises without a booted context (coordinator item a: real, narrow)

- Where: `src/horus_builtin/task/horus_task.py:161`. `BaseTask.workflow` calls `HorusContext.get_context()`, which raises `RuntimeError("HorusContext is not set...")` when the context was never booted (`context.py:154-162`).
- Impact: the read happens even when every digest is supplied, so `manifest_matches(manifest, digests)`, `is_complete()` and `_fingerprint()` now raise outside a booted context. Before ae7a20e they worked there.
- tc-plugins is not affected: `artifact_cache.py:558/640` runs inside task middleware with a booted context. Scripts, CLI checks and tests that never boot are affected; `test_map.py:2077` needed a fixture for this.
- Second effect: with a booted context but no running workflow, the fallback uses `self.cache or "deep"` and ignores the workflow's `cache`. A `fast` workflow's tasks then never match outside a run. That is a false miss, never a wrong skip.
- Minimal fix: tolerate the missing context and resolve it only when a digest is needed.
  ```python
  try:
      wf = self.workflow
  except RuntimeError:
      wf = None
  ```
  An alternative is to make `BaseTask.workflow` return `None` when no context is set, which matches its docstring ("None outside of a workflow run").

### L2. A loop inside a subworkflow body ignores the body's or subworkflow's `cache`

- Where: `expander.py:306` sets `controller.cache` on an inlined `LoopController`. `loop.py:406` builds each iteration from `body_template` only. The next-iteration controller (`loop.py:470-482`) does not carry `cache` either.
- Failure scenario: a subworkflow with `body.cache: deep` inside a `fast` workflow. Its loop iterations run `fast`, which contradicts the documented subworkflow precedence.
- Minimal fix: in `_build_body_clone`, add `if self.cache is not None: data.setdefault("cache", self.cache)`, and pass `cache=self.cache` to the `LoopController(...)` at `loop.py:470`.

### L3. `_seed_folder_digest` lets a `list_dir` error fail the map

- Where: `src/horus_builtin/workflow/map.py:1027-1031`. The slot digests are guarded but the listing is not.
- Failure scenario: a transient SFTP error, or a `TargetLostError` right after `_settle`, fails a map whose clones all succeeded. The seed is only a saving, as `_digest_slot` says.
- Minimal fix: wrap the `list_dir` in `try/except Exception: return`.

### L4. An `assert` in `_tree_digest` escapes `ArtifactStore.digest`

- Where: `src/horus_runtime/core/artifact/store.py:148`. `digest` catches only `ValueError` and `IndexError` (`:265`).
- Failure scenario: a folder is changed while `find` walks it, so one path is listed as both a file and a directory. The `AssertionError` then propagates through `wf.digest`, then `source_digests`, and fails the task instead of returning `None` (a miss). The run cache does not keep it.
- Minimal fix: `if not isinstance(child, dict): raise ValueError(path)`.

### L5 (plausible). Invalidation is per `location_id`, but two location ids can share a filesystem

- Where: `src/horus_runtime/core/workflow/base.py:774-790` (`forget_digests`).
- Problem: the SSH `location_id` includes the user (`ssh_target.py:338-340`). `user1@host` and `user2@host`, SSH and Slurm on one cluster, or `local://` and SSH to localhost reach the same paths under different ids. A writer at one id does not drop a digest cached under the other, so the rest of the run can see a stale value and wrongly skip.
- Whether this matters depends on whether such mixed targets are supported in one run. I did not find a guard either way.

### L6 (plausible). `_copy_keeping_mtime` can turn a successful copy into an error

- Where: `src/horus_builtin/target/local.py:49-57`.
- Problem: when `dst` already exists and is owned by another user (group-writable), `copyfile` succeeds but `os.utime` raises `PermissionError`.
- Minimal fix: `with contextlib.suppress(OSError): os.utime(...)`.

### Info. In fast mode the leaf preimages are not fully domain-separated

A file under 1 MiB has the leaf `fast:` + `H(content)`. If its content is exactly `horus-stat\n<size>:<mtime>`, it collides with a large file's stat leaf. If its content is `horus-tree\n...`, it collides with a subfolder's digest. Only crafted content triggers this. If the format ever changes, tag the content leaf, for example `fast2:` with `H(b"horus-blob\n" + content)`. That needs the hash composed in Python, because `sha256sum` cannot prepend the tag.

## Coordinator item d: the run cache is active only while RUNNING

Nothing relies on the cache for correctness outside a run.

- `run()` sets `RUNNING` before the workflow middleware and `_run`. It flips status only after they return (`base.py:1947-1988`).
- Manifests, including the map's re-fingerprint with `_streamed_digests`, are written inside `_run`.
- Outside RUNNING, `digest` passes through uncached and `seed_digest` is a no-op. The only cost is extra reads.
- A digest that is in flight when `finally` clears the dict is never re-inserted: the owner deletes or keeps only its own future (identity check).
- tc-plugins `output_digests` (`side_product_upload.py:463`) and `CachedOutputTransfer` call `ArtifactStore.digest` directly (deep), not the run cache.
- The only behaviour change outside a run is the L1 fallback mode, which can only cause a false miss.

## Checked and found correct

- **Seeded map root equals a recompute:**
  - Symlinked slots are skipped on both sides: `list_dir` skips symlinks by contract (`target/base.py:688`, `local.py:413`) and `find -type f` does not follow them.
  - Empty slots are pruned on both sides.
  - Dropped slots are forgotten before removal (`map.py:1355`).
  - A failed removal is digested fresh.
  - Any non-slot entry, or a `None` slot digest, means no seed.
  - Clones run on `self.target.idle_copy()`, at the same location, so fast mtimes are the same files.
- **Waiter loop:**
  - An owner that raises or is cancelled wakes its waiters, which retry, unless they were cancelled themselves.
  - `None` and exceptions are never kept.
  - `seed_digest` replacing an in-flight entry is safe.
  - `forget_digests` covers equal, under and above paths, every generation and every mode.
  - `_nested` is right for `/`.
- **Carried digests:**
  - `digest_mode` gates them correctly.
  - Map clones share the map's mode, so `_input_digests` and `_staged_digests` are in the right mode.
  - A pinned input reads once (cache hit in `_fingerprint`).
  - `is_complete` reusing `_staged_digests` matches what `run()` fingerprints.
- **Shell command:**
  - `p=$(shlex.quote)` is used, and every use is `"$p"`.
  - Every failure path yields a non-zero exit or an unparsable record, which gives `None`, never a wrong digest. This covers an unreadable file, a missing `-z`, an old `sha256sum` that escapes names, a stat failure, and a missing `horus-content`.
  - GNU `-z` disables name escaping.
  - `-size -1048576c` is exact in bytes.
  - `%T@` cut at `.` equals `stat %Y` for positive times.
  - Deep file digests are unchanged (`$h < "$p"` gives the same hex as `$h path`).
- **Propagation:**
  - Subworkflow precedence and the unset-body round trip (`_dump_body` pops `cache`) are right.
  - Map clones inherit `cache` through the dump.
  - `cache` stays out of the config hash.

Not checked: other targets' `list_dir` symlink handling, and `generation` on releasable plugin targets after `idle_copy`. I did not run the tests.

## Fix round 1

The findings are numbered in review order: C1 = H1, C2 = H2, C3 = M1, C4 = L1, C5 = L2, C6 = L3, C7 = L4, C8 = L5, C9 = L6.

| Finding | Outcome |
|---|---|
| C1 (H1, horus-ssh `preserve`) | Fixed in the horus-ssh repo, not here. |
| C2 (H2, digest taken while the writer runs) | `f605638`: the scheduler `finally` forgets the task's outputs again. Test: `test_digest_taken_while_writer_runs_is_dropped`. `48c1e42`: a map is exempt, because its clones forget their own slots and the map then seeds its folder digest, which the second forget would drop. |
| C3 (M1, `cache:` next to `sub:`) | `9519e5d`: `lower_subworkflow_entry` keeps `cache`, and `yaml-schema.md` no longer says it is dropped. |
| C4 (L1, `_fingerprint` without a context) | `53467a9`: falls back to the uncached `ArtifactStore` path. `test_config_hash_unchanged_without_new_fields` no longer boots a context, so it covers this. The second effect (a booted context with no running workflow ignores the workflow's `cache`) is unchanged: it can only cause a false miss. |
| C5 (L2, loop inside a subworkflow) | `a54ff71`: each body clone without its own `cache` takes the controller's, and the next controller check carries it. The YAML `loop:` lowering still drops an outer `cache:`, as decided. |
| C6 (L3, `list_dir` in `_seed_folder_digest`) | `77e2143`: a failed listing seeds nothing, and the readers digest the folder themselves. |
| C7 (L4, `assert` in `_tree_digest`) | `9dc7104`: raises `ValueError`, which `digest` turns into `None`. |
| C8 (L5, two location ids share a filesystem) | Rejected: the fix is not trivial. It needs a filesystem identity across location ids (user, SSH vs Slurm, `local://` vs SSH to localhost), and nothing defines whether such mixed targets are supported in one run. |
| C9 (L6, `os.utime` after the copy) | `cc538d3`: setting the mtime is best-effort (`contextlib.suppress(OSError)`). |

Also in this round: `cb3b520` (performance P1) makes each consumer take its source digests before the transfer. That made `test_chained_clone_recovers_lost_source_item` racy, because its node loss could now land during B[0]'s copy. `b504b74` makes the loss wait until B[0] is done.

## Re-review round 2

Scope: `git log 53686bc..HEAD` (f605638..dadbeee). I only read the code. I ran no tests. Line numbers are at `dadbeee`.

No high finding remains. The fixes for C2-C7 and C9 do what they claim. The two medium findings below are about `cb3b520`, which now digests at the source on every first run.

### R2-M1. Composite tasks that never fingerprint now digest their inputs (`cb3b520`)

- Where: `src/horus_builtin/workflow/scheduler.py:414`, `if task.outputs: await input_digests()`.
- Problem: `BranchRouter` (`branch.py:102`), `LoopController` (`loop.py:62`) and `SubworkflowExpander` (`expander.py:59`) have outputs (a sentinel, markers, port placeholders). Their `is_complete` is always `False`, and their `_run` override never calls `_fingerprint`. Before `cb3b520` they never digested anything. Now each one digests every fingerprinted input at its source, on every run, and throws the result away.
- Failure scenario: an expander whose ports are wired to a large upstream folder, in a `deep` workflow with `body.cache: fast`. The expander reads the whole folder in `deep` mode. No inner task shares that cache key, so the read is pure waste (the same order as the 108 GB rnaseq folders). A digest error on such a source (an SSH error, for example) also now fails a task that used to succeed. Magnitude is plausible: it depends on the edges into the expander being `transfer` edges, and I did not trace that end to end.
- Minimal fix: override `fingerprinted_inputs` to return `[]` in those three classes (they never compare a fingerprint). Alternatively, gate line 414 on a `ClassVar` that only fingerprinting tasks set.

### R2-M2 (plausible). The pre-transfer digest touches a lost source machine (`cb3b520`)

- Where: `src/horus_runtime/core/workflow/base.py:1694-1710` (`source_digests`). Unlike `_transfer_input` (`:1465`), it does not call `_recover_if_lost`/`leases.output_lost` before it uses `source_target`.
- Problem: before `cb3b520`, a first-run consumer whose producer's machine was lost (not released, so not relocated) never touched that machine. The transfer saw `output_lost`, recovered from the evacuator and digested the staged copy. Now `input_digests()` runs `ArtifactStore.digest` on the dead `source_target` first. A releasable target that provisions on any use while down (the model `_Node` says terraform follows, `test_map.py:2387-2395`) brings up a whole machine just to find the path missing (`None`). That machine is outside the lease's view of the lost generation. A target that raises `TargetLostError` instead is caught by the retry loop in `_execute_on_target` as if the *consumer's* node were lost. Each retry digests again and nothing records a relocation, so the consumer can use up `MAX_TARGET_LOSS_RETRIES` and fail where it used to recover. The same path already existed on re-runs, through `is_complete_before_transfer`. `cb3b520` widens it to every first run.
- Minimal fix: in `source_digests`, before digesting, if `source is not None and source.task_id is not None and source.artifact is not None and source_target.releasable and self.leases.output_lost(str(source.task_id), source.artifact.id, source_target)`, set `digests[artifact.id] = None` and `continue`. That is a miss, so the run digests the staged copy as before.

### R2-L1. A map's drop removal is no longer backstopped by a forget at finish (`48c1e42`)

- Where: `src/horus_builtin/workflow/map.py:1356-1357` forgets the dropped slots *before* `_remove_slots`. Until `48c1e42`, the scheduler's forget at finish (`scheduler.py:459`) also covered the map. Maps are now exempt.
- Failure scenario: an independent task digests an ancestor of the map's root (or the root in a mode no edge consumer uses) while the `rm -rf` runs. Its digest still includes the dropped slot, and it stays in the cache. A later reader of that path, ordered after the map, gets this stale value. If the last run ended with that slot present and unchanged, the reader matches its manifest and wrongly skips. The seed only overwrites the root key, and only in the consumer modes. When the seed bails out (listing failure, non-slot entry), it overwrites nothing. This is narrow: it needs a drop, a concurrent reader of an ancestor, and the exact window.
- Minimal fix: call `wf.forget_digests(self.target, removed)` again after `await _remove_slots(...)`. That is before the seed, so the seed is unaffected.

### Verified, no issue

- **`48c1e42` keying:**
  - `dispatches_children` is a `ClassVar`, `True` only on `MapTask`. Clones are built from the body kind, so they still forget at finish. A nested-map clone is a `MapTask` and seeds its own folder digest, which is correct.
  - `_digest_slot` runs after `run_task(clone)` returns, so after the clone's forget at finish.
  - A map retried after its own loss forgets its outputs again at dispatch start (`scheduler.py:441`), and the seed key carries the old generation.
  - `_rerun_lost` and the early reruns all finish before `_settle` and the seed. No clone runs after the seed.
  - A chained consumer writes into its own root (`into` is under its own working directory), and its own forgets cover that root.
- **`cb3b520` fingerprint consistency:**
  - The manifest is now written from the source digests whenever none is `None`. That is the same source the next run's `is_complete_before_transfer` compares against, in the same mode (`cache_mode(task)` on both sides).
  - Pinned inputs are excluded from `_staged_digests` and digested in place on the acquired `task.target` on both sides. The second read is a cache hit.
  - Streamed inputs are not in `fingerprinted_inputs`.
  - A `None` (a storage-only relocated source raises `NotImplementedError`, which becomes `None`) falls back to staged digests, as before.
  - `skip_if_complete: false` only changes where the digest is read.
  - A map clone's preset `_staged_digests` is overwritten with values that agree with it. The broadcast inputs carry the map's digest through `EdgeSource.digest`.
- **Dropping `leases.pulling`:** correct. `member_started` (`lease.py:445`) or `repull` (`:479`) already put the task in `_pulling`. The context manager's exit would call `_done_pulling` before the transfer, which would let a source be released mid-transfer.
- **`5623adf`:** equal to `_nested` for normalized absolute keys. It handles root `/` (`under == ("/",)` drops every absolute key, as before), trailing slashes (`PurePosixPath` strips them) and siblings (`/a/b` vs `/a/bc`: neither the parents set nor the `"/a/b/"` prefix matches). There are two differences. A relative drop now also drops a `"."` key, which is safe because it drops more. A `//a` drop no longer drops a `/` key. Both are unreachable in practice.
- **`a54ff71` and `9519e5d`:** precedence is still task > body > subworkflow task > workflow. The expander fills `controller.cache` only when it is unset (`expander.py:306`). A loop body clone takes the controller's mode only when its template has none. The next controller carries it. The shorthand copies `cache` into the `kind: subworkflow` dict, which the expander reads as `self.cache`. `cache` stays out of the config hash, so there is no fingerprint churn.
- **`b504b74`:** the test still checks its stated case (loss after A[1] finished and before B[1] read it). B[1]'s pre-transfer digest still runs after the loss. The hidden overlap was the harness `rmtree` under B[0]'s copy of A[0], a loss in the middle of a copy that the test never meant to cover. `cb3b520` widened that window by adding B[0]'s source digest before its copy. In the harness the node is re-provisioned at once, so the digest only returns `None`. The real-world consequence of a source that stays down is R2-M2, not this test.
- **`53467a9`, `9dc7104`, `6901113`, `77e2143`, `cc538d3`:** each does what it claims. `asyncio.to_thread` re-raises `ValueError`/`IndexError` into the existing `except`.

Not checked: whether edges into a `SubworkflowExpander` port are `transfer` edges (R2-M1's magnitude), and how real releasable plugin targets behave when used while down (R2-M2).

## Fix round 2

| Finding | Outcome |
|---|---|
| R2-M1 (composite tasks digest their inputs) | `9673be9`: `BranchRouter`, `LoopController` and `SubworkflowExpander` return `[]` from `fingerprinted_inputs`, so the pre-transfer digest reads nothing for them. Nothing else used their fingerprinted inputs: their `_run` never fingerprints and their `is_complete` is always `False`. Tests: `test_inputs_are_never_fingerprinted` (router, loop), `test_expander_inputs_are_never_fingerprinted`. |
| R2-M2 (pre-transfer digest touches a lost machine) | `6118112`: `source_digests` records `None` for a source whose output `_recover_if_lost` would recover (the same check, now `_source_lost`), without digesting it. The transfer then recovers it and the run digests the staged copy, as before `cb3b520`. Test: `test_source_digest_skips_a_lost_machine`. |
| R2-L1 (drop removal not backstopped by a forget) | `ce1c5f1`: the map forgets the dropped slots again after `_remove_slots`, before the seed. Test: `test_digest_taken_during_the_drop_removal_is_forgotten`. |

The bytes-read numbers are unchanged: plain deep 48 MiB before, 8 MiB after, fast 0 bytes; map deep 8 MiB from the slots and 0 commands on the map root.
