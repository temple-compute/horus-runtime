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
