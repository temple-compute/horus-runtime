# Cache modes: backend design

Runtime design for `cache-modes`, written against HEAD `e0db354` (code = `43fc77e`). Inputs: `cache-modes-plan.md`, `cache-modes-prd.md`, `cache-modes-codebase.md`. Line numbers are at that HEAD. Nothing here was run except small shell probes of the digest commands (GNU coreutils 9.4, findutils, busybox 1.36).

Summary of the change:

- One target-side command per artifact and mode. Python turns its NUL-separated output into a Merkle digest.
- A run cache on the workflow: one digest per (location, generation, path, mode) per run, shared through futures. Dispatching a task drops the cache entries around its outputs.
- Map clones digest their own slot after they succeed. That digest goes into the run cache. After `_settle` the map composes its folder digest from the slot digests and seeds it, so consumers and `map.py:901` read nothing.
- `cache: deep | fast` on the workflow and on tasks.
- mtime is kept on local copies and SFTP/scp copies.

## 1. Digest format

### 1.1 Values

| Artifact | `deep` | `fast` |
|---|---|---|
| File | `<sha256 of content>` (unchanged) | `fast:<leaf hex>` |
| Folder | `deep:<tree hex>` | `fast:<tree hex>` |

- **Fast leaf.** For a file under 1 MiB (`size < 1048576`), the leaf hex is the content sha256. For a larger file it is `sha256(b"horus-stat\n%d:%d" % (size, mtime_s))`, with mtime in whole seconds.
- **Tree hex.** `sha256(b"horus-tree\n" + entries)`. Each entry is `name + b"\0" + child_digest + b"\0"`. Entries are sorted by raw name bytes.
  - `child_digest` is the child's own public digest from the table: a plain hex or `fast:` value for a file, a `deep:` or `fast:` value for a sub-folder.
  - Names are raw bytes, so no locale or Python `str` ordering is involved.
  - A child folder whose digest equals the **empty-folder digest** (`prefix + sha256(b"horus-tree\n")`) is left out. So a subtree with no regular file never counts.
  - The root folder is never left out. An empty folder digests to the empty-folder digest.
- **Modes never collide.** Deep files are plain 64-hex, deep folders start with `deep:`, and everything fast starts with `fast:`. `digest_mode(d)` returns `"fast"` when `d` starts with `fast:`, else `"deep"`.
- **Old digests.** A folder digest without a prefix is old format. A future format change takes a new prefix (`deep2:`).
- **No explicit kind field in an entry (deviation from codebase §3).** The composer in §3 only has each slot's public digest, and a fast digest does not say whether it was a file or a folder. Instead the kind is carried by the child digest itself: by the prefix in deep mode, and by the tagged preimage (`horus-tree` / `horus-stat`) in fast mode. Two entries could collide only if a file's bytes were exactly one of those tagged preimages. Both sides apply the same rule, so composing and recomputing still agree in that case.
- **Path independence.** A digest does not depend on its path: the root's own name is never hashed. Source and staged copies therefore agree, as today.
- **Symlinks** are handled the same way on both sides:
  - The artifact's own path is followed, through `[ -d ]`, `cd`, `$h <` and `stat -L`.
  - Inside a folder, symlinks are skipped, because `find -type f` neither matches nor descends them. This is what happens today.
  - The map composition in §3 excludes symlinked slots because `list_dir` skips symlinks. This matches a root recompute.

### 1.2 Target-side command

There is one `run_command_sync` per artifact and mode, and so one round trip. `P` is `shlex.quote(path)`. The prelude is the same for both modes, and only the marked lines differ. The whole script is wrapped as `{ ...; } 2>/dev/null`, as `store.py:90` does today.

```sh
export LC_ALL=C
h=sha256sum; command -v sha256sum >/dev/null || h='shasum -a 256'
st='stat -L -c %s:%Y'; stat -L -c %s / >/dev/null 2>&1 || st='stat -L -f %z:%m'
export h st
z=; printf '' | $h -z >/dev/null 2>&1 && z=1
hashes() {
  if [ -n "$z" ]; then find . -type f "$@" -print0 | xargs -0 -r $h -z
  else find . -type f "$@" -exec sh -c 'for f; do printf "%s  %s\0" "$($h < "$f" | cut -c1-64)" "$f"; done' sh {} +
  fi
}
stats() {
  if find . -maxdepth 0 -printf '' 2>/dev/null; then find . -type f -printf '%s:%T@ %p\0'
  else find . -type f -exec sh -c 'for f; do printf "%s %s\0" "$($st "$f")" "$f"; done' sh {} +
  fi
}
if [ -d P ]; then
  cd P || exit 1
  printf 'horus-tree\0'
  hashes                                                         # deep
  stats && printf 'horus-content\0' && hashes -size -1048576c    # fast
elif [ -f P ]; then
  printf 'horus-file\0'
  $h < P                                                         # deep
  s=$($st P) || exit 1; printf '%s\0' "$s"                       # fast
  if [ "${s%%:*}" -lt 1048576 ]; then $h < P; fi                 # fast
else
  exit 1
fi
```

What each tool path does:

- **GNU, fast path.**
  - Content records come from `sha256sum -z` (coreutils 8.30 or later). It ends each record with NUL and does not escape names.
  - Stat records come from `find -printf`, with `%T@` cut at the `.` in Python, so whole seconds.
  - `xargs -0 -r` is used already today.
- **Fallbacks.** They are slower, with one fork per file, and they are only used where the fast path is missing: coreutils before 8.30 (RHEL 7), busybox, and macOS (`shasum`, BSD `stat -f`, no `find -printf`).
  - Names travel as `-exec ... {} +` arguments, so they are NUL-safe.
  - Hashing goes through stdin (`$h < "$f"`), so names never pass through the hasher's output.
- **Probe results.** On this host the GNU path and a busybox run gave the same records for a tree with `big` (1.1 MB), `a`, `sub/new\nline` and a symlink, including `1100000:1793885581` from both stat paths. They also gave the same content hexes as the forced loop fallback.
- **Sorting** is done in Python on the raw bytes, which is equivalent to an `LC_ALL=C` byte sort. The target therefore needs no `sort -z`, which busybox and macOS may lack. `LC_ALL=C` is still exported so that no tool localizes its output.
- **Failure means `None`, as today.** That covers:
  - a non-zero exit (a missing path, an unreadable file through `xargs`'s exit 123, a vanished file);
  - an empty or short hex from the loop fallback;
  - any record that does not parse.

### 1.3 Python composition (`store.py`)

New module-level names in `src/horus_runtime/core/artifact/store.py` (pure, no I/O):

```python
CacheMode = Literal["deep", "fast"]
CACHE_MODES: tuple[CacheMode, ...] = ("deep", "fast")
SMALL_FILE = 1 << 20  # fast mode hashes files below this by content
_TREE_TAG = b"horus-tree\n"
_STAT_TAG = b"horus-stat\n"
_PREFIX: dict[CacheMode, str] = {"deep": "deep:", "fast": "fast:"}

def digest_mode(digest: str) -> CacheMode:
    return "fast" if digest.startswith("fast:") else "deep"

def compose_folder_digest(mode: CacheMode, children: Mapping[str | bytes, str]) -> str:
    """Digest of a folder whose entries digest to *children* (name -> digest)."""
    empty = _PREFIX[mode] + hashlib.sha256(_TREE_TAG).hexdigest()
    entries = sorted(
        (os.fsencode(name), digest) for name, digest in children.items() if digest != empty
    )
    body = b"".join(name + b"\0" + digest.encode() + b"\0" for name, digest in entries)
    return _PREFIX[mode] + hashlib.sha256(_TREE_TAG + body).hexdigest()

def _leaf(mode, size: int, mtime: int, content: str | None) -> str | None:
    if mode == "deep":
        return content
    if size < SMALL_FILE:
        return None if content is None else "fast:" + content
    return "fast:" + hashlib.sha256(_STAT_TAG + b"%d:%d" % (size, mtime)).hexdigest()

def _tree_digest(mode, leaves: dict[bytes, str]) -> str:
    """Fold relative paths (b"./a/b") -> leaf digest into nested folders."""
    tree: dict = {}
    for path, digest in leaves.items():
        *dirs, name = path.removeprefix(b"./").split(b"/")
        node = tree
        for part in dirs:
            node = node.setdefault(part, {})
        node[name] = digest
    def walk(node: dict) -> str:
        return compose_folder_digest(
            mode, {n: walk(c) if isinstance(c, dict) else c for n, c in node.items()}
        )
    return walk(tree)
```

Parsing in `ArtifactStore.digest`:

- Split `out` on `b"\0"`, and drop the last element only when it is empty. The file branch ends with the hasher's `"<hex>  -\n"`, which has no NUL.
- `horus-file`:
  - deep: the first 64 chars of the next record are the hex, and the result is the plain hex.
  - fast: the next record is `size:mtime`, optionally followed by a hex record, and the result is `_leaf(...)`.
- `horus-tree`:
  - deep: each record is `hex(64) + ("  " | " *") + path`.
  - fast: the records up to `horus-content` are `size:mtime path`, split at the first space. The records after it are content records as in deep, joined to the stat records by path. A file under 1 MiB with no content record gives `None`. A content record for a larger file is ignored.
  - Then `_tree_digest`.
- Any malformed record (a non-hex digest, a bad integer, an unknown header) gives `None`.

**Composing equals recomputing, by construction.**

- A root recompute is `walk(tree(root))`, which is `compose_folder_digest(mode, {n: walk(subtree n)})`.
- For a top-level entry `n` that is a folder, the per-slot digest taken on `root/n` runs the same `find` from inside `n`. It sees the same files under the same relative paths without the `n/` prefix, and their leaves depend only on bytes, size and mtime. So it equals `walk(subtree n)`.
- For an entry that is a file, the `horus-file` branch builds the same `_leaf` (the same content hash, and the same size and whole-second mtime).
- A folder with no regular file is absent from the recompute, and its per-slot digest is the empty-folder digest, which `compose_folder_digest` drops.
- Symlinked and non-listed entries are handled by §3.
- The only known difference: GNU `%T@` cut at the `.` and `stat %Y` can differ for mtimes before 1970 with a fractional part. Such files would only ever cause extra re-runs.

`ArtifactStore.digest` becomes `async def digest(self, artifact, mode: CacheMode = "deep") -> str | None`. The default keeps tc-plugins and horus-ssh callers compiling. Their folder digests change format, see §8. `_DIR_MARKER` goes. The ponytail comment at `store.py:84` stays true: empty dirs and modes are not part of a digest.

## 2. Run cache

### 2.1 State and API (`BaseWorkflow`, `src/horus_runtime/core/workflow/base.py`)

The new PrivateAttr sits next to `_relocations` (`:332`):

```python
_digests: dict[tuple[str, int, str, CacheMode], "asyncio.Future[str | None]"] = PrivateAttr(default_factory=dict)
"""This run's input digests by (location id, machine generation, path on target, mode)."""
```

Methods, placed after `clear_relocations` (`:387`):

```python
def cache_mode(self, task: BaseTask) -> CacheMode:
    """*task*'s cache mode: its own, else this workflow's."""
    return task.cache or self.cache

def _digest_key(self, target, artifact, mode):
    return (target.location_id, target.generation,
            str(PurePosixPath(target.path_on_target(artifact))), mode)

async def digest(self, target: BaseTarget, artifact: BaseArtifact, mode: CacheMode) -> str | None:
    """
    ArtifactStore(target).digest(artifact, mode), taken once per run: concurrent
    callers share one, a None or an error is never kept. Outside a run, not cached.
    """
    if self.status is not WorkflowStatus.RUNNING:
        return await ArtifactStore(target).digest(artifact, mode)
    key = self._digest_key(target, artifact, mode)
    while (pending := self._digests.get(key)) is not None:
        try:
            return await asyncio.shield(pending)
        except CancelledError:
            # The digest we waited on failed: take our own unless we were cancelled.
            task = asyncio.current_task()
            if task is None or task.cancelling():
                raise
    pending = asyncio.get_running_loop().create_future()
    self._digests[key] = pending
    value = None
    try:
        value = await ArtifactStore(target).digest(artifact, mode)
        pending.set_result(value)
        return value
    finally:
        pending.cancel()  # no-op once done; wakes waiters to retry after an error
        if value is None and self._digests.get(key) is pending:
            del self._digests[key]

def seed_digest(self, target, artifact, mode: CacheMode, digest: str) -> None:
    """Record *digest* as this run's digest of *artifact* on *target*, without reading it."""
    if self.status is not WorkflowStatus.RUNNING:
        return
    future = asyncio.get_running_loop().create_future()
    future.set_result(digest)
    self._digests[self._digest_key(target, artifact, mode)] = future

def forget_digests(self, target: BaseTarget, paths: Iterable[str]) -> None:
    """
    Drop the digests at, under or above *paths* at *target*'s location, any
    generation: something is about to write there.
    """
    drop = [str(PurePosixPath(p)) for p in paths]
    location = target.location_id
    # shortcut: a scan per dispatch (~1 ms at 10k entries); index by location and sorted path if maps grow past that.
    for key in [k for k in self._digests if k[0] == location and any(_nested(k[2], p) for p in drop)]:
        del self._digests[key]

def _nested(a: str, b: str) -> bool:  # module level
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")
```

Notes:

- **Mirrors existing code.** The waiter loop is the same as the in-flight transfer table (`strategy.py:126-158`), so the behaviour on a cancel or an error is the same.
- **Owner semantics.** An owner that returns `None` hands `None` to its current waiters but keeps nothing for later callers. An owner that raises (`TargetLostError`, `NotImplementedError` from a storage-only target) wakes its waiters through `cancel()`, and each waiter then takes its own digest.
- **The `RUNNING` guard** keeps digests from being reused across runs, and outside a run (tests, a long-lived tc-os worker that calls `source_digests` between runs). It is a single check.
- **Reset.** In `BaseWorkflow.run`'s `finally` (`:1847-1849`), add `self._digests.clear()` next to `ctx.workflow = None`. Cancelled owners clean up after themselves.
- **Generation in the key.** A replaced machine (`_restage`, target-loss retry) gets new keys with no explicit drop. Old entries become unreachable until the run ends.

### 2.2 Invalidation hook points

1. **Dispatch.** In `src/horus_builtin/workflow/scheduler.py` `_transfer_and_run`, right before `await target.dispatch(task)` (`:430`), inside the `try`:
   ```python
   # The task writes its outputs from here: digests of them, or of folders around them, are stale.
   workflow.forget_digests(target, [target.path_on_target(o) for o in task.outputs])
   ```
   Every dispatch goes through this function: tasks, map clones, early re-runs, `_rerun_lost`, target-loss retries and the map itself. So this one hook covers re-runs, retries, in-place writers (an output equal to or nested in another task's folder) and `transfer=False` writers. A map's own dispatch drops its root and every slot under it.
2. **Real copies.** In `BaseWorkflow._transfer_input`, after `artifact.path = transfer_art.path` (`:1407`):
   ```python
   if source_target.location_id != task.target.location_id:
       # A real copy rewrote the destination (an unpack can swap a folder in place).
       self.forget_digests(task.target, [task.target.path_on_target(artifact)])
   ```
   A same-location transfer is only a repoint to the source path. Forgetting there would throw away the source's own entry, so it is skipped.
3. **Dropped slots.** In `MapTask._settle`, before `_remove_slots` (`map.py:1271`), forget the same paths that are removed (see §3).

Writers to undeclared paths are not tracked. Document this next to the `fast` caveats.

### 2.3 Readers that use it

- **`BaseWorkflow.source_digests`** (`:1519-1564`).
  - Take `mode = self.cache_mode(task)` once, and log `_("Task %(task_name)s fingerprints its inputs in %(mode)s cache mode.")` at debug level (PRD Should 2).
  - Pinned branch (`:1543-1547`): `await self.digest(task.target, artifact, mode)`.
  - Carried digest (`:1550`): use `source.digest` only when `digest_mode(source.digest) == mode`. Otherwise fall through. This is how `EdgeSource.digest` becomes mode-aware: through the prefix, with no new field. Update its docstring (`:124-130`) to say so.
  - Source branch (`:1556-1563`): `await self.digest(source_target, src_artifact or artifact, mode)`, still inside `try/except NotImplementedError`. Move `path_on_target` inside that `try`; `_digest_key` calls it.
  - This one change covers the task-skip middleware, the bound-task middleware, `is_complete_before_transfer`, `manifest_matches` (tc-plugins) and `map.py:901`, because all of them go through `_Once` (`scheduler.py:201`, `:321`) and `source_digests`.
- **`HorusTask._fingerprint` fallback** (`horus_task.py:162-165`). This handles staged copies, pinned inputs and the map clone's item:
  ```python
  wf = self.workflow
  ...
  if artifact.id not in digests:
      digests[artifact.id] = (
          await wf.digest(self.target, artifact, wf.cache_mode(self))
          if wf is not None
          else await ArtifactStore(self.target).digest(artifact, self.cache or "deep")
      )
  ```
  Pinned inputs are now read once per run. `source_digests` and `_fingerprint` hit the same key: the task's declared target and the target acquired from the pool are copies of the same machine, with the same location, generation and `path_on_target`.
- **`HorusTask.is_complete(None)` (the stale re-run from `BaseTask.run`, `task/base.py:452`).** Replace `digests = None` (`horus_task.py:278`) with `digests = self._staged_digests`. When the scheduler staged the inputs, the check reuses the source digests. Pinned inputs and any missing ones go through the fallback, which hits the cache. Outside the scheduler, `_staged_digests` is `None` and the behaviour is as before.
- **Map clones** (`map.py:800-809`, `:1487`). These need no change. The shared inputs still ride on `EdgeSource.digest` and `_staged_digests`, in the map's mode, and a clone has the same mode as its map (§4). Items are digested through the fallback, which hits the cache. When the collection is another map's output on the same location, the item path is that map's slot path, so the item hits the slot digest the producing clone seeded (§3).

### 2.4 Seeding

- **Map composed digest.** After `_settle` (§3).
- **Slot digests.** By the clones (§3).
- **Evacuation and recovery (PRD Should 1).**
  - Mechanism: in `relocate_output` (`:376`), after recording the relocation, carry every cached digest of the producer's original copy onto the new location:
    ```python
    task = next((t for t in self.tasks if t.id == task_id), None)
    output = ... (by output_id)
    generation = self.leases.produced_generation(task_id, output_id)
    old = (task.target.location_id,
           task.target.generation if generation is None else generation,
           str(PurePosixPath(task.target.path_on_target(output))))
    for mode in CACHE_MODES:
        future = self._digests.get((*old, mode))
        if future is not None and future.done() and not future.cancelled() and future.result() is not None:
            self.seed_digest(source.target, source.artifact, mode, future.result())
    ```
    Wrap the block in `except NotImplementedError: return`, for storage-only `path_on_target`. Skip it when `source.target` or `source.artifact` is `None`.
  - Deviation from the PRD: both modes are carried. The PRD said `fast` only when mtimes are kept. But the seeded value is the producer's own digest of the same bytes, so consumers in this run agree whichever copy they read. A later run that digests the copy directly can only cause an extra re-run, never a wrong skip.
  - This is optional work. Do it last.
- **Root inputs** are cached like everything else, through `source_digests`. This is acceptable: a user who edits a root input during a run gets the first digest.

## 3. Map: per-slot digests and the composed folder digest

All changes are in `src/horus_builtin/workflow/map.py`.

**Deviation from the plan.** Per-slot digests are not stored on `_ItemStream` or in map state. They are run-cache entries keyed by the slot path. That way the existing invalidation (dispatch, generation, dropped slots) covers re-runs, `_restage` and loss with no extra clearing code, and a chained clone reading the slot hits the same entry.

1. **Which modes.** Slot digests are taken in the modes the map's output is read in, so a `deep` map whose consumers are `fast` does not read its data for nothing:
   ```python
   def _consumer_modes(self, wf: BaseWorkflow) -> set[CacheMode]:
       """The cache modes this map's output is read in."""
       tasks = {t.id: t for t in wf.tasks}
       return {wf.cache_mode(tasks[e.target]) for e in wf.edges
               if e.source == self.id and e.transfer and e.target in tasks}
   ```
   Clone edges are `transfer=False` and do not count. A chained map consumer is a normal transfer edge. With no consumer, no mode is needed, and nothing is digested or composed.

   In `_map_items`, compute `modes = self._consumer_modes(wf)` just before `run_clone = partial(` (`:827`) and pass it as a new argument: `partial(self._run_item, stream, waits, modes, partial(execute_task, ...))`. `_run_item` (`:918`) gains `modes: set[CacheMode]` before `run_task`. `_start_lost_reruns` and `_rerun_lost` reuse `run_clone`, so they need no change.
2. **When the slot is digested.** This happens in `_run_item`'s `else:` branch (`:967`), after `run_task(clone)` succeeded or the clone skipped complete, and **before** `stream.ready(slot)`:
   ```python
   else:
       await self._digest_slot(clone, modes)
       stream.ready(slot)
       return None
   ```
   ```python
   async def _digest_slot(self, clone: BaseTask, modes: set[CacheMode]) -> None:
       """Digest *clone*'s slot on its own machine while its data is hot, for the folder digest and its readers."""
       wf = self.workflow
       if wf is None:
           return
       for mode in sorted(modes):
           try:
               await wf.digest(clone.target, clone.outputs[0], mode)
           except Exception as exc:  # noqa: BLE001 - only a saving: a reader digests it itself
               horus_logger.log.debug(...)
   ```
   - The `try` is required. An `else:` clause is outside the `except` clauses, so an error raised there would otherwise fail the clone.
   - Doing it before `ready` means a chained clone that starts on this slot finds the digest done, or in flight and shared.
   - `clone.target` is the clone's declared target (an `idle_copy` of the map's target), on the same machine and generation. The map is still running and holds the lease, so the machine is alive.
   - Dropped and upstream-dropped clones are not digested.
3. **When a slot is re-run, restaged or lost.**
   - A re-run (`_take_rerun`, early re-runs, `_rerun_lost`) dispatches the clone again. The §2.2 hook drops the slot and the root, and the next success digests the slot again.
   - `_restage` and loss bring a new generation, so the old keys are never hit.
   - Nothing to add in `_take_rerun` or `_restage`.
4. **Dropped slots.** In `_settle` (`:1271`), forget the slots before removing them:
   ```python
   removed = [str(root / slot) for slot in self._dropped_slots]
   if (wf := self.workflow) is not None:
       wf.forget_digests(self.target, removed)
   await _remove_slots(self.target, removed)
   ```
5. **Composing the folder digest.** Call it right after `await self._settle(...)` (`:886`) and before `stream.close(ok=True)` (`:897`), so that a chained consumer (which waits on `closed`) and its `:901` digest find the root seeded:
   ```python
   await self._settle(root, labels, clones, results)
   await self._seed_folder_digest(wf, clones, modes)
   ```
   ```python
   async def _seed_folder_digest(self, wf, clones, modes) -> None:
       """
       Record this map's folder digest, composed from its slots' digests
       instead of reading the folder again: equal to a recompute by construction.
       """
       if not modes:
           return
       output = self.outputs[0]
       by_slot = {c.id[len(self.id) + 1 : -1]: c for c in clones}
       listed = [e.name for e in await self.target.list_dir(self.target.path_on_target(output))]
       # Something other than a slot is in the folder: readers digest it themselves.
       if not listed or any(name not in by_slot for name in listed):
           return
       for mode in sorted(modes):
           children = {}
           for name in listed:
               clone = by_slot[name]
               digest = await wf.digest(clone.target, clone.outputs[0], mode)
               if digest is None:
                   break
               children[name] = digest
           else:
               wf.seed_digest(self.target, output, mode, compose_folder_digest(mode, children))
   ```
   How this covers the codebase §3 cases:
   - **Failed removal.** A dropped slot that stays is still listed. It is digested now (a read of that slot only) and composed, as a recompute would include it. Anything listed that is not a slot means no seed. An empty listing (a missing root) means no seed.
   - **Empty directories.** Pruned through the empty-folder digest rule (§1.3).
   - **Symlinked slot.** `list_dir` skips symlinks (`target/base.py:690`, `local.py:402`), and the recompute skips them too.
   - **Names.** NUL-separated byte names on the target side, and `os.fsencode` on `list_dir` names. Slot names are indices or upstream slot names, so they are valid UTF-8.
   - **Re-runs.** Handled by item 3.
   - **Mode.** The same mode on both sides, by key.

   A slot with no cached digest (its digest failed) is read on its own, never the whole folder.
6. **`map.py:901`** needs no code change. `wf.source_digests(self, ..., only=streams.keys())` digests each source map's root through the cache and hits the digest that the source map seeded before closing its stream. The source map took its slot digests in this map's mode, because this map is one of its consumers. `_write_manifest` (`:1340`) stays as is.
7. **Consumers.** A consumer reaches the map's digest through `source_digests`, then `wf.digest`, then the seeded entry. `EdgeSource.digest` stays reserved for digests carried by a relocation (tc-plugins) and for the map's own shared inputs (`:806`).

Known gap: when the consumer is a `SubworkflowExpander` that has not expanded yet, `_consumer_modes` sees the expander's mode, not that of its inner tasks (§4). A mismatch costs one read of the root by the consumer, never a wrong result.

## 4. The `cache` field

- **`BaseWorkflow`**, after `failure_policy` (`base.py:272`): `cache: CacheMode = "deep"`. Its docstring covers deep and fast, the fast caveats, and that a task overrides it.
- **`BaseTask`**, after `skip_if_complete` (`task/base.py:269`): `cache: CacheMode | None = None`. Its docstring: `None` inherits.
- **Validation.** Both classes get the same `before` validator, so the message is the translated one, not pydantic's `Literal` text:
  ```python
  @field_validator("cache", mode="before")
  @classmethod
  def _check_cache(cls, value: object) -> object:
      if value is not None and value not in CACHE_MODES:
          raise CacheModeError(value)
      return value
  ```
  In `src/horus_runtime/core/workflow/exceptions.py`:
  ```python
  class CacheModeError(WorkflowError):
      """Raised when ``cache`` is not one of the cache modes."""
      def __init__(self, value: object):
          super().__init__(
              _("Unknown cache mode %(value)s. Use one of: %(modes)s.")
              % {"value": repr(value), "modes": ", ".join(CACHE_MODES)}
          )
  ```
  - `WorkflowError` is a `ValueError` (`exceptions.py:30`), so pydantic wraps it into a `ValidationError` with this text.
  - `None` on the workflow still hits pydantic's own type error. That is fine.
  - Spanish: `"Modo de caché %(value)s desconocido. Use uno de: %(modes)s."`, in the usted form used in the `.po` file.
  - Debug line from §2.3: `"La tarea %(task_name)s calcula la huella de sus entradas en modo de caché %(mode)s."`
  - Both strings live in `src/horus_runtime`, so `make babel-refresh` extracts them.
- **Effective mode.**
  - `BaseWorkflow.cache_mode(task)` returns `task.cache or self.cache`. `HorusTask` without a workflow uses `self.cache or "deep"`.
  - Map clones inherit through `model_dump` in `_clone` (`map.py:1446-1470`), so a clone's `cache` equals the map's, including `None`.
  - Loop iterations inherit through `body_template` (`loop.py:406`), meaning the body's own `cache`.
  - `lower_loop_entry` (`loop.py:497-531`) drops a `cache:` set on the outer loop entry, as it drops every other outer key. Docs: set `cache:` inside `loop.body`.
- **Subworkflow (D4).** In `SubworkflowExpander._build_inner_tasks`, next to the `skip_if_complete` propagation (`expander.py:298`):
  ```python
  # The inner task's own mode, else the body's, else this task's; unset falls to the workflow's.
  if task.cache is None:
      task.cache = self.body.cache if "cache" in self.body.model_fields_set else self.cache
  ```
  - The precedence is: inner task, then body `cache:`, then the subworkflow task's own `cache:`, then the parent workflow. This follows the existing `skip_if_complete` pattern and answers PRD question 4.
  - `model_fields_set` keeps a body that sets nothing from forcing `deep` over a `fast` parent.
  - Nested bodies chain through the inner expander's own `cache`.
- **Config hash.** `cache` is not part of it. The hash covers only runtime, executor, code and `_config_extra` (`horus_task.py:170-182`; map `_config_extra` at `:1310`). The mode is captured by the digest prefix instead. Add a test that setting `cache` leaves `config_hash` unchanged.
- **JSON schema.** It is automatic (`AutoRegistry`), with `enum: [deep, fast]`.

## 5. Copies that keep mtime

1. **`src/horus_builtin/target/local.py`.** Use a module helper at `:295` (`put_file`, the `Path` branch) and at `:317` (`get_file_to`):
   ```python
   def _copy_keeping_mtime(src: Path, dst: Path) -> None:
       """Copy *src* to *dst* with its times but not its mode: a read-only source must not make the copy read-only."""
       shutil.copyfile(src, dst)
       stat = src.stat()
       os.utime(dst, ns=(stat.st_atime_ns, stat.st_mtime_ns))
   ```
   This is chosen over `copy2` because `copy2` also copies the mode bits, and a later `put_file` over a read-only copy would then fail.
2. **horus-ssh (plugin change, separate repo).** Code is at `~/Developer/horus-ssh-perf` (branch `perf/task-launch`, 8a8c339). The `origin/main` (v0.0.9) lines are in brackets. `~/Developer/horus-ssh` is an initial-commit checkout (4064392) with no copy calls.
   - `src/horus_ssh/target/ssh_target.py:766` [`:724`]: `await sftp.put(str(content), str(remote_path), preserve=True)`.
   - `src/horus_ssh/target/ssh_target.py:839` [`:797`]: `await sftp.get(str(remote_path), str(local_path), follow_symlinks=True, preserve=True)`.
   - `src/horus_ssh/transfer/ssh_transfer.py:176` [`:172`]: `asyncssh.scp(..., recurse=is_dir, preserve=True)`.
   - `preserve=True` also sets permissions. That is safe here because `put_file(Path)` writes packages at unique paths (`store.py:163-171`), never over an existing file.
   - SFTP v3 and scp keep whole seconds, which is what `fast` uses (D1).
   - `LocalToSSHTransfer` (perf branch, `ssh_transfer.py:216-219`) keeps calling `ArtifactStore.digest(artifact)`, which stays deep by default. Both ends compute the new format, so its comparison still works.
3. **Nothing else.**
   - tar (`folder.py:107-151`) already keeps whole-second mtimes on extract.
   - `FolderArtifact.write` uses `copytree`, which uses `copy2`.
   - horus-slurm and horus-docker do not copy.
   - `BaseTarget.get_file_to` (default `write_bytes`) has no remote mtime to keep.
   - The `folder.py:141` package hash is out of scope (PRD Won't).

## 6. Interface contract

| Where | New or changed |
|---|---|
| `horus_runtime.core.artifact.store` | `CacheMode`, `CACHE_MODES`, `SMALL_FILE`, `digest_mode(digest) -> CacheMode`, `compose_folder_digest(mode, children: Mapping[str \| bytes, str]) -> str`; `ArtifactStore.digest(artifact, mode: CacheMode = "deep") -> str \| None` (new format, see §1) |
| `horus_runtime.core.workflow.exceptions` | `CacheModeError(WorkflowError)` |
| `BaseWorkflow` | field `cache: CacheMode = "deep"`; PrivateAttr `_digests`; `cache_mode(task)`, `async digest(target, artifact, mode)`, `seed_digest(target, artifact, mode, digest)`, `forget_digests(target, paths)`; `source_digests` and `relocate_output` use them; `run()` clears `_digests` |
| `BaseTask` | field `cache: CacheMode \| None = None` with a validator |
| `EdgeSource` | no new field; `digest` is used only when `digest_mode(digest)` equals the consumer's mode (docstring) |
| `HorusTask` | `_fingerprint` uses `wf.digest`; `is_complete(None)` uses `_staged_digests` |
| `MapTask` | `_consumer_modes`, `_digest_slot`, `_seed_folder_digest`; `_run_item(stream, waits, modes, run_task, clone)` |
| scheduler | `forget_digests` before `target.dispatch` |
| `SubworkflowExpander` | inner `cache` propagation |
| `LocalTarget` | `put_file(Path)` and `get_file_to` keep atime and mtime |

## 7. Work, files and tests

### 7.1 Files per change

| Change | Files |
|---|---|
| Digest format | `src/horus_runtime/core/artifact/store.py` |
| Field, validation, i18n | `core/workflow/base.py`, `core/task/base.py`, `core/workflow/exceptions.py`, `src/horus_runtime/locale/es/LC_MESSAGES/horus_runtime.po` (`make babel-refresh`, then fill 2 `msgstr`) |
| Run cache, readers, invalidation | `core/workflow/base.py`, `src/horus_builtin/task/horus_task.py`, `src/horus_builtin/workflow/scheduler.py` |
| Map | `src/horus_builtin/workflow/map.py` |
| Subworkflow | `src/horus_builtin/workflow/subworkflow/expander.py` |
| mtime | `src/horus_builtin/target/local.py`; horus-ssh (plugin) |
| Docs (docs WP, last) | `.claude/skills/horus-workflow/references/yaml-schema.md` (top-level block `:9-35`, task block `:45`, skip section `:250`, an "Upgrading" line), `references/execution.md:51-70`, `SKILL.md:34-36`; horus-docs PR |

### 7.2 Tests

All are unit tests. Spies wrap `ArtifactStore.digest`, which now takes `(self, artifact, mode="deep")`.

| File | Update or add |
|---|---|
| `tests/unit/artifact/test_store.py` | Update `test_folder_digest_tracks_contents_and_names` (`:140`) to expect `deep:`. Add: file digest unchanged (`hashlib.sha256(content)`). **Composed equals recomputed**: `compose_folder_digest(mode, {n: digest(root/n)})` equals `digest(root)` for both modes, over nested dirs, an empty subdir and an empty top-level dir, a symlinked subdir and file inside, a top-level file, names with newline, backslash, space and non-UTF-8 bytes, and an empty root. **Fallbacks**: put a shim dir first on `PATH` (monkeypatch) where `sha256sum` exits 1 on `-z` and `find` exits 1 on `-printf`, and check the same digests as without it. Run the same check through busybox applets when `shutil.which("busybox")` finds it. **Fast semantics** on a 2 MiB file: same-size content change with the mtime put back (`os.utime(ns=...)`) gives the same fast digest (the documented false hit) and a different deep one; a rewrite in the same whole second with different nanoseconds gives the same fast digest (documented); `touch +1 s` changes fast and not deep; a small file rewritten with the same size and mtime changes fast. Package then unpackage keeps whole-second mtimes, and the fast digest of the copy equals the source's. |
| `tests/unit/target/test_local_target.py` | `put_file(Path)` and `get_file_to` keep `st_mtime_ns`; the mode of a 0o444 source is not copied. |
| `tests/unit/workflow/test_digest_cache.py` (new) | Set `wf.status = RUNNING` by hand. Concurrent `wf.digest` calls make 1 store call (gated spy). `None` not kept (2 calls give 2 commands). An exception not kept (raise `TargetLostError` once, then succeed). `forget_digests` drops `/a` and `/a/b` for path `/a/b`, keeps `/a/c` and `/x`, keeps other locations, and drops every generation. `seed_digest` is used without a read. The cache is cleared after `run()` and is a pass-through when not running. **Bytes-read benchmark**, defined below. |
| `tests/unit/workflow/test_edges.py` (`TestSourceDigests`, `:787`) | A carried deep digest is used in deep mode with `_NoShellTarget` (no read), and ignored in fast mode (reads). A carried `fast:` digest is used in fast mode. A pinned input is digested once across `source_digests` and `_fingerprint`. |
| `tests/unit/workflow/test_scheduler.py` | Update the spy at `:1696`. N consumers of one root folder give 1 digest call per (path, mode). An in-place writer (task `W` whose output is the folder, ordered after the first consumer, then a second consumer) makes the second consumer see the new digest. |
| `tests/unit/task/test_builtin_task.py` | `is_complete()` with `_staged_digests` set makes no digest call for non-pinned inputs. `config_hash` unchanged with `cache: fast`. An invalid `cache:` on task and workflow gives a `ValidationError` naming `deep, fast`. |
| `tests/unit/workflow/test_map.py` | Update the spy at `:556`; re-check counts, which only go down. Keep `:3956` (the chained manifest equals a fresh `ArtifactStore.digest`): it now proves composed equals recomputed end to end. Add: consumers of a map folder make no digest call on the root path; the seeded root equals a fresh digest after a dropped slot, an upstream-dropped slot, an empty slot and a symlinked slot; a re-run slot is digested again; no consumers means no slot digests. |
| `tests/unit/workflow/test_subworkflow.py` | Precedence: inner, then body, then expander, then parent. A body without `cache:` does not force deep. |

**Bytes read** has no counter today, so it is defined here. A spy wraps `ArtifactStore.digest`. On each call it adds the sizes of the regular files under `store.target.path_on_target(artifact)` (`os.walk`, without following links below the top), counting all files in `deep` and only files under `SMALL_FILE` in `fast`. Each call is one target command, so the spy counts commands too.

- **Data:** a folder of 4 files of 2 MiB each (8 MiB) and N = 6 consumer shell tasks on `LocalTarget`, each with one output so that a manifest is written.
- **Before (baseline):** patch `BaseWorkflow.digest` to bypass the cache (`return await ArtifactStore(target).digest(artifact, mode)`). Assert 6 × 8 MiB.
- **After:**
  - `deep`: 8 MiB and 1 command.
  - `fast`: 0 bytes and 1 command.
  - A second run, where everything skips, gives the same numbers.
- **Map variant:** 4 slots of 2 MiB each and 3 consumers of the map folder. Assert `deep` = 8 MiB (the clones only) and zero calls on the map root.

**Running on this host:** single process only, `nice -n 19 uv run pytest -p no:xdist <files>`. Then `make lint type-check babel-check`. No Docker and no E2E until the parent says the bench is done.

## 8. tc-os / tc-plugins impact (out of repo, follow-ups)

1. **Required before tc-os bumps its vendored runtime** (`~/Developer/tc-os/horus-runtime`).
   - tc-plugins must treat a recorded digest **of a folder artifact** with no `deep:`/`fast:` prefix as missing. In `CachedOutputTransfer`, skip the verification or re-record (`s3_transfer.py:533-565`). In `artifact_cache.py:578-586`, do not carry it as `EdgeSource.digest`.
   - Without that, the first pull of an old cached folder **fails the consumer**.
   - File digests are unchanged and stay valid.
2. **`CachedOutputTransfer` stays deep.** It already calls `ArtifactStore.digest(artifact)`, which defaults to deep.
3. **`output_digests`** (`side_product_upload.py:449-473`) can call `wf.digest(task.target, output, "deep")` and save a full read per output, including composed map folders. But a map only composes in its consumers' modes. If tc-os wants deep records for maps that are read `fast`, it has to add `deep` to `_consumer_modes`, which costs a read (see §9).
4. **`fast` in tc-os.** Today it re-runs consumers of lazily skipped outputs (`S3CacheTarget` has no shell), unless tc-plugins records `fast:` digests too. Recording them is cheap (stat only). S3 single-file restores also lose mtime. Document `fast` as not useful in tc-os until then (PRD question 7).
5. **tc-os editor control**: see `cache-modes-ux.md`. The schema exposes the field automatically.

## 9. Open questions (the code does not decide these)

1. Should a map take slot digests in `deep` even when no consumer is deep, so that tc-plugins `output_digests` can reuse the composed digest? It costs a full read of the map's output in fast-only workflows. The proposal is no, until tc-os uses `wf.digest`.
2. Should `lower_loop_entry` pass an outer `cache:` through to the controller and the body? Today it drops it silently, like the other outer keys. The proposal is docs only in v1.
