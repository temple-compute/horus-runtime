# Cache modes: merged design

The single source the engineers implement from. Inputs: `cache-modes-plan.md`, `-prd.md`, `-codebase.md`, `-backend.md`, `-ux.md` (tc-os editor, doc only). Line numbers are at `43fc77e`. Where this file and an input disagree, this file wins.

## Summary

- One target-side command per artifact and mode prints NUL-separated records; Python folds them into a Merkle digest (`deep:`/`fast:` prefixes, deep file digests unchanged).
- A run cache on the workflow digests each (location, generation, path, mode) once per run; dispatching a task forgets entries at, under or above its outputs.
- Map clones digest their own slot right after they succeed; after `_settle` the map composes and seeds its folder digest, so consumers and `map.py:901` read nothing.
- `cache: deep | fast` on the workflow (default `deep`) and on tasks (unset = inherit). `fast` = path + size + whole-second mtime, plus content for files under 1 MiB.
- Local copies and horus-ssh SFTP/scp copies keep mtime.

## YAML surface

```yaml
name: rnaseq
kind: horus_workflow
cache: fast                # optional; deep (default) | fast. Default for every task.
tasks:
  - id: star
    kind: horus_task
    cache: deep            # optional; overrides the workflow. Unset = inherit.
    ...
  - id: per_sample
    kind: horus_map
    cache: fast            # clones inherit the map's value
    ...
  - id: refine
    loop:
      body:
        cache: deep        # set it inside loop.body; a cache: on the outer loop entry is dropped
        ...
  - id: qc
    kind: subworkflow
    cache: deep            # applies to inner tasks that set nothing and whose body sets nothing
    body:
      cache: fast          # body default for its inner tasks
      tasks: [...]
```

- Precedence: task's own `cache:`; for a subworkflow inner task, then the body's `cache:`, then the subworkflow task's `cache:`; then the workflow's `cache:`; then `deep`.
- Invalid value: `CacheModeError` (a `WorkflowError`, so pydantic wraps it in `ValidationError`): `Unknown cache mode 'x'. Use one of: deep, fast.` (Spanish entry in WP4).
- `cache` is not in the task config hash. The digest prefix carries the mode, so switching a task's mode re-runs it once.
- `fast` caveats for the docs: same-size rewrite in the same second is a false hit; `touch` or a copy that drops mtime is a false miss; NFS attribute caching on the head node; writers to undeclared paths are not tracked by the run cache.

## Migration effect

- **Re-runs once after the upgrade:** every task with a folder input (folder digests become `deep:<merkle>`), including chained maps (their manifest records streamed folder digests), and every task whose effective mode changes. Tasks with only file inputs do not re-run (deep file digests unchanged).
- Note goes in the 0.9.0 GitHub release notes, an "Upgrading" line in `yaml-schema.md`, and the horus-docs running-workflows page. No CHANGELOG file.
- **tc-plugins follow-up (out of repo, required before tc-os bumps its vendored runtime `~/Developer/tc-os/horus-runtime`):** treat a recorded digest of a folder artifact with no `deep:`/`fast:` prefix as missing: skip or re-record in `CachedOutputTransfer` (`s3_transfer.py:533-565`), do not carry it as `EdgeSource.digest` in `artifact_cache.py:578-586`. Without this, the first pull of an old cached folder fails the consumer. `CachedOutputTransfer` stays deep (it calls `digest(artifact)`, default deep). Optional: `output_digests` (`side_product_upload.py:449`) calls `wf.digest(task.target, output, "deep")`.

## Interface contract

### `src/horus_runtime/core/artifact/store.py`

```python
CacheMode = Literal["deep", "fast"]
CACHE_MODES: tuple[CacheMode, ...] = ("deep", "fast")
SMALL_FILE = 1 << 20                      # fast hashes files with size < this by content
def digest_mode(digest: str) -> CacheMode  # "fast" if digest.startswith("fast:") else "deep"
def compose_folder_digest(mode: CacheMode, children: Mapping[str | bytes, str]) -> str
async def ArtifactStore.digest(self, artifact, mode: CacheMode = "deep") -> str | None
```

Digest values (`H` = sha256 hex):

| Artifact | deep | fast |
|---|---|---|
| File | `H(content)` (unchanged, no prefix) | `fast:` + (`H(content)` if size < 1 MiB, else `H(b"horus-stat\n%d:%d" % (size, mtime_s))`) |
| Folder | `deep:` + tree hex | `fast:` + tree hex |

- Tree hex = `H(b"horus-tree\n" + b"".join(name + b"\0" + child_digest.encode() + b"\0"))`, entries sorted by raw name bytes. `child_digest` is the child's public digest from the table (file or sub-folder). A child equal to the mode's empty-folder digest (`prefix + H(b"horus-tree\n")`) is left out; the root is never left out.
- No kind field: the prefix (deep) or the tagged preimage (fast) carries it.
- Path-independent: the root's own name is never hashed.
- Symlinks: the artifact's own path is followed; inside a folder, symlinks are skipped (`find -type f`), as today.
- An unprefixed folder digest is old format. A future format change takes a new prefix (`deep2:`).
- `_DIR_MARKER` goes. Any failure (non-zero exit, short or malformed record) returns `None`, as today.

Target-side command: one `run_command_sync` per artifact and mode, wrapped in `{ ...; } 2>/dev/null`, `P = shlex.quote(path_on_target)`. Lines marked `# deep` / `# fast` are emitted only for that mode.

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

Parsing: split on `\0` (drop only a trailing empty element). `horus-file` deep: first 64 chars of the next record. `horus-file` fast: `size:mtime`, then an optional hex record. `horus-tree` deep: records `hex64 + ("  "|" *") + path`. `horus-tree` fast: `size:mtime path` records (split at the first space, `%T@` cut at `.`) up to `horus-content`, then content records joined by path; a file < 1 MiB without a content record gives `None`. Fold paths into nested dicts and compose bottom-up with `compose_folder_digest`. Sorting is in Python on bytes, so the target needs no `sort -z`.

### `BaseWorkflow` (`src/horus_runtime/core/workflow/base.py`)

- Field after `failure_policy` (`:272`): `cache: CacheMode = "deep"`, with a `field_validator("cache", mode="before")` raising `CacheModeError` for any non-`None` value outside `CACHE_MODES`.
- PrivateAttr after `_relocations` (`:332`): `_digests: dict[tuple[str, int, str, CacheMode], asyncio.Future[str | None]]`, key = `(target.location_id, target.generation, str(PurePosixPath(target.path_on_target(artifact))), mode)`.
- `cache_mode(task) -> CacheMode`: `task.cache or self.cache`.
- `async digest(target, artifact, mode) -> str | None`: pass-through to `ArtifactStore(target).digest(artifact, mode)` unless `self.status is WorkflowStatus.RUNNING`. Otherwise one in-flight future per key, using the waiter loop of `strategy.py:134-158` (`asyncio.shield`, `CancelledError` + `task.cancelling()`); the owner's `finally` calls `pending.cancel()` and deletes the entry when the value is `None` or it raised. `None` and exceptions are never kept.
- `seed_digest(target, artifact, mode, digest) -> None`: store a done future; no-op unless running.
- `forget_digests(target, paths: Iterable[str]) -> None`: drop every entry at `target.location_id` (any generation, any mode) whose path is equal to, under, or above one of `paths`. Module helper `_nested(a, b)`. Linear scan, with a `shortcut:` comment.
- `run()` `finally` (`:1847`): `self._digests.clear()`.
- `source_digests` (`:1519`): `mode = self.cache_mode(task)` once, debug log `_("Task %(task_name)s fingerprints its inputs in %(mode)s cache mode.")`; pinned branch and source branch call `self.digest(...)` (move `path_on_target` inside the `NotImplementedError` try); a carried `source.digest` is used only when `digest_mode(source.digest) == mode`, else fall through. Update the `EdgeSource.digest` docstring (`:124-130`).
- `_transfer_input`, after `artifact.path = transfer_art.path` (`:1407`): when `source_target.location_id != task.target.location_id`, `self.forget_digests(task.target, [task.target.path_on_target(artifact)])`.

### Other files

- `BaseTask` (`src/horus_runtime/core/task/base.py`, after `skip_if_complete` `:269`): `cache: CacheMode | None = None`, same validator.
- `src/horus_runtime/core/workflow/exceptions.py`: `class CacheModeError(WorkflowError)`, message `_("Unknown cache mode %(value)s. Use one of: %(modes)s.")` with `repr(value)` and `", ".join(CACHE_MODES)`.
- `HorusTask._fingerprint` (`horus_task.py:162-165`): fallback uses `wf.digest(self.target, artifact, wf.cache_mode(self))` when `self.workflow` is set, else `ArtifactStore(self.target).digest(artifact, self.cache or "deep")`. `is_complete` (`:278`): `digests = self._staged_digests` instead of `None`.
- `scheduler.py` `_transfer_and_run`, just before `await target.dispatch(task)` (`:430`), inside the `try`: `workflow.forget_digests(target, [target.path_on_target(o) for o in task.outputs])`. Covers tasks, clones, early re-runs, `_rerun_lost`, target-loss retries and the map itself.
- `SubworkflowExpander._build_inner_tasks` (`expander.py:298-299`, next to `skip_if_complete`): `if task.cache is None: task.cache = self.body.cache if "cache" in self.body.model_fields_set else self.cache`.
- `LocalTarget` (`local.py:295` `put_file` Path branch, `:317` `get_file_to`): helper `_copy_keeping_mtime(src, dst)` = `shutil.copyfile` + `os.utime(dst, ns=(st_atime_ns, st_mtime_ns))`. Not `copy2`: copying a read-only mode would break a later `put_file` over it.
- `MapTask` (`map.py`):
  - `_consumer_modes(wf) -> set[CacheMode]`: modes of edge targets with `e.source == self.id and e.transfer`. Empty set = no slot digests, no seed.
  - `_run_item(stream, waits, modes, run_task, clone)`; `modes` computed before `run_clone = partial(...)` (`:827`). In the `else:` branch (`:967`), `await self._digest_slot(clone, modes)` before `stream.ready(slot)`.
  - `_digest_slot(clone, modes)`: `wf.digest(clone.target, clone.outputs[0], mode)` per mode inside `try/except Exception` with a debug log (an error there must not fail the clone).
  - `_settle` (`:1271`): `wf.forget_digests(self.target, removed)` before `_remove_slots(self.target, removed)`.
  - `_seed_folder_digest(wf, clones, modes)`, called right after `await self._settle(...)` (`:886`), before the conditional `stream.close(ok=True)` (`:887-897`): `list_dir` the root; if empty or any name is not a clone slot, return; else per mode compose from `wf.digest(clone.target, clone.outputs[0], mode)` per listed slot (cache hits) and `wf.seed_digest(self.target, output, mode, ...)`; skip the mode if any slot digest is `None`.
  - `map.py:901` unchanged: `source_digests` hits the root the source map seeded before closing its stream.

### Run cache rules (summary)

| Event | Effect |
|---|---|
| First reader of a key | runs the command; concurrent readers await the same future |
| Task dispatch | forget its outputs' paths (equal, under, above) at its location |
| Real cross-location copy | forget the destination path |
| Map drops slots at settle | forget those slot paths |
| Machine replaced (`_restage`, target-loss retry) | new generation, new keys; nothing to drop |
| Clone success or skip-complete | slot digested per consumer mode (seeds the slot key) |
| Map settled | root digest composed and seeded |
| Run ends, or workflow not `RUNNING` | cache cleared / bypassed |

## Work packages

Run WP1, WP2, WP3, WP4 sequentially in this worktree (each builds on the previous one's API and their write scopes overlap in tests). WP-SSH is in another repo and can run any time. Each WP ends green on its tests and on `uv run make lint babel-check` (`lint` = ruff check + format check + mypy; `type-check` is mypy alone). Tests: `nice -n 19 uv run pytest -p no:xdist <files>`, single process.

### WP1: digest format, `cache` field, mtime-keeping local copies

- Write scope: `src/horus_runtime/core/artifact/store.py`, `src/horus_runtime/core/workflow/exceptions.py`, `src/horus_runtime/core/workflow/base.py` (field + validator only), `src/horus_runtime/core/task/base.py`, `src/horus_builtin/workflow/subworkflow/expander.py`, `src/horus_builtin/target/local.py`; tests `tests/unit/artifact/test_store.py`, `tests/unit/target/test_local_target.py`, `tests/unit/task/test_builtin_task.py`, `tests/unit/workflow/test_subworkflow.py`.
- Depends on: none.
- Tests:
  - `test_store.py:140` expects `deep:`. File deep digest equals `hashlib.sha256(content).hexdigest()`.
  - Composed equals recomputed, both modes: `compose_folder_digest(mode, {n: digest(root/n)})` == `digest(root)` over nested dirs, an empty subdir and an empty top-level dir, a symlinked subdir and file, a top-level file, names with newline, backslash, space and non-UTF-8 bytes, and an empty root.
  - Fallbacks: a shim dir first on `PATH` (monkeypatch) where `sha256sum -z` and `find -printf` exit 1 gives the same digests; same through busybox applets when `shutil.which("busybox")`.
  - Deep vs fast at digest level, on a 2 MiB file: same-size content change with mtime restored (`os.utime(ns=...)`) keeps fast, changes deep; same second, different nanoseconds keeps fast; mtime +1 s changes fast, not deep; a < 1 MiB file rewritten with same size and mtime changes fast.
  - Fast reads no big file: a 2 MiB file chmod 000 gives a fast digest and deep `None` (skip when `os.geteuid() == 0`).
  - mtime across pack/unpack: `package` then `unpackage` on `LocalTarget` keeps whole-second mtimes, and the copy's fast digest equals the source's.
  - `put_file(Path)` and `get_file_to` keep `st_mtime_ns`; a 0o444 source does not make the copy read-only.
  - Invalid `cache:` on task and workflow raises `ValidationError` naming `deep, fast`; `config_hash` unchanged with `cache: fast`.
  - Subworkflow precedence: inner, then body, then subworkflow task, then workflow; a body without `cache:` does not force deep over a fast parent.

### WP2: run cache, readers, invalidation

- Write scope: `src/horus_runtime/core/workflow/base.py`, `src/horus_builtin/task/horus_task.py`, `src/horus_builtin/workflow/scheduler.py`; tests: new `tests/unit/workflow/test_digest_cache.py`, `tests/unit/workflow/test_edges.py`, `tests/unit/workflow/test_scheduler.py`, `tests/unit/task/test_builtin_task.py`.
- Depends on: WP1.
- Tests:
  - `test_digest_cache.py` (set `wf.status = WorkflowStatus.RUNNING` by hand): concurrent `wf.digest` on one key gives 1 store call (gated spy); `None` not kept; an exception (`TargetLostError` once) not kept and waiters retry; `forget_digests("/a/b")` drops `/a` and `/a/b`, keeps `/a/c`, `/x` and other locations, drops every generation; `seed_digest` read without a call; pass-through when not running; cleared after `run()`.
  - Once per run (`test_scheduler.py`, update the spy at `:1696` to accept `mode`): N = 5 consumers of one root folder give exactly 1 digest command per (path, mode).
  - In-place writer: task `W` writes into the folder after consumer 1 and before consumer 2; consumer 2 sees the new digest.
  - Deep vs fast invalidation, task level (two runs each): same-size content change with mtime restored re-runs a deep consumer and skips a fast one; `touch` re-runs the fast one.
  - `test_edges.py` `TestSourceDigests`: a carried deep digest is used in deep mode with `_NoShellTarget`, ignored in fast; a carried `fast:` digest is used in fast; a pinned input is digested once across `source_digests` and `_fingerprint`.
  - `test_builtin_task.py`: `is_complete()` with `_staged_digests` set makes no digest call for non-pinned inputs.
  - Bytes-read benchmark (below), plain-task variant.

### WP3: per-slot digests and composed map digest

- Write scope: `src/horus_builtin/workflow/map.py`; tests `tests/unit/workflow/test_map.py`.
- Depends on: WP2.
- Tests: update the spy at `:556`; counts only go down. `:3956` (chained manifest equals a fresh `ArtifactStore.digest`) must pass unchanged: composed == recomputed end to end. Add: consumers of a map folder make no digest command on the root path; the seeded root equals a fresh `ArtifactStore.digest(root, mode)` after a dropped slot, an upstream-dropped slot, an empty slot, a symlinked slot, and a failed slot removal (patch `_remove_slots` to leave it: seed still equals recompute, or no seed); a re-run slot is digested again; a map with no consumers digests no slot. Bytes-read benchmark, map variant.

### WP4: i18n and docs (last)

- Write scope: `src/horus_runtime/locale/**` (run `make babel-refresh`, fill the 2 Spanish `msgstr`, usted form: `Modo de caché %(value)s desconocido. Use uno de: %(modes)s.` and `La tarea %(task_name)s calcula la huella de sus entradas en modo de caché %(mode)s.`), `.claude/skills/horus-workflow/**` (`references/yaml-schema.md`: top-level block, task block next to `skip_if_complete`, skip section, loop note, subworkflow precedence, `fast` caveats, "Upgrading" line; one line each in `references/execution.md:51-70` and `SKILL.md:34-36`).
- Depends on: WP3. Tests: `make babel-check`; no code tests.

### WP-SSH: mtime on horus-ssh copies (plugin repo, apply only if small)

- Repo: the horus-ssh branch that ships with 0.9.0. Lines for `perf/task-launch` 8a8c339 (`~/Developer/horus-ssh-perf`), `main` in brackets.
- Files: `src/horus_ssh/target/ssh_target.py:766` [`:724`] `sftp.put(..., preserve=True)`; `:839` [`:797`] `sftp.get(..., follow_symlinks=True, preserve=True)`; `src/horus_ssh/transfer/ssh_transfer.py:176` [`:172`] `asyncssh.scp(..., recurse=is_dir, preserve=True)`. Tests: `tests/unit/test_ssh_target.py` fakes `_get` (`:1455`) and `_put` (`:2157`) take no `preserve` argument and must accept it; assert `preserve=True` on the scp recorder (`tests/unit/test_ssh_transfer.py:104`) and on one put/get fake.
- Depends on: none. Run `nice -n 19 uv run pytest -p no:xdist tests/unit/test_ssh_target.py tests/unit/test_ssh_transfer.py` and that repo's lint.

### Bytes-read benchmark (WP2 plain variant, WP3 map variant)

- Measure: a spy wraps `ArtifactStore.digest`. Each call is one target command. Bytes for a call = sum of `st_size` of regular files under `store.target.path_on_target(artifact)` (`os.walk`, not following symlinks), all files in `deep`, only files with size < `SMALL_FILE` in `fast`. This counts what the command reads by its definition; the WP1 chmod-000 test proves fast does not read big files.
- Cross-check: a `LocalTarget.run_command_sync` spy (pattern `test_map.py:2245-2305`) counting commands that contain `horus-tree` equals the spy's call count.
- Data: a folder of 4 files of 2 MiB (8 MiB), N = 6 consumer shell tasks on `LocalTarget`, each with one output so a manifest is written.
- Before: the same workflow with `BaseWorkflow.digest` patched to `return await ArtifactStore(target).digest(artifact, mode)` (no cache, the per-consumer path of 43fc77e). Assert 6 commands and 6 x 8 MiB.
- After: deep 1 command and 8 MiB; fast 1 command and 0 bytes. A second run (everything skips) gives the same numbers.
- Map variant: 4 slots of 2 MiB, 3 consumers of the map folder: deep 8 MiB total (the 4 clone slot digests), zero commands on the map root path.

## Deviations from the plan and PRD

1. Per-slot digests are run-cache entries keyed by slot path, not fields on `_ItemStream` (plan). Reason: dispatch, generation and dropped-slot invalidation then cover re-runs, `_restage` and loss with no extra code, and chained clones hit the same entry.
2. `map.py:901` needs no change (plan: compose from item digests there). Reason: the source map seeds its root before closing its stream; `:901` goes through `source_digests` and hits it.
3. `_restage` and recovery are handled by the generation in the key, not by a drop on producer start (plan). Reason: they do not re-run the producer (codebase §1).
4. No kind field in tree entries (plan: `(name, kind, child digest)`). Reason: the composer only has public digests; kind rides on prefix or tagged preimage.
5. `fast` hashes files under 1 MiB by content and uses whole-second mtime (plan: no content read). Reason: PRD D1, D2 (tar/scp/SFTP precision; generated files rewritten each run).
6. Deep file digests stay unprefixed; deep folders get `deep:` (plan: prefix fast only). Reason: PRD D3; file inputs and tc-os file records stay valid.
7. Slot digests are taken only in the modes the map's consumers use; no consumer, no digest (not in plan/PRD). Reason: a deep map read only in fast must not read its data. Gap: consumers added later (an unexpanded subworkflow, loop iterations) may read the root once themselves.
8. Two more invalidation hooks besides dispatch: real cross-location copies and dropped slots (PRD names dispatch only). Reason: an unpack swaps the destination in place; a dropped slot leaves the folder. Known cost: the copy hook also fires when the strategy skipped or joined a copy, which can cost one extra read of a staged copy on the fallback path, never a wrong skip.
9. Cache cleared in `run()`'s `finally` and bypassed unless `RUNNING` (PRD: clear where `lease.py:925` clears relocations). Reason: also covers callers outside a run (tc-os worker, tests).
10. Subworkflow precedence adds the subworkflow task's own `cache:` between the body and the workflow (PRD D4 had inner, body, parent). Reason: mirrors the `skip_if_complete` propagation; settles PRD Q4.
11. Custom `CacheModeError` instead of pydantic's `Literal` message. Reason: PRD Must 7 asks for a translated message.
12. `LocalTarget` copies use `copyfile` + `os.utime`, not `copy2` (PRD allowed either). Reason: `copy2` copies mode bits.
13. Deferred: seeding on `relocate_output` (PRD Should 1). Reason: optional; without it a consumer of an evacuated copy reads it once per run (cached), and tar keeps whole-second mtimes so fast agrees. See question 6.
14. Map `stream.close(ok=True)` at `:897` is conditional (deferred when the map can be retried on target loss), so the seed goes right after `_settle`, not "before close" only.

## Open questions

1. Whole-second mtime for `fast` (PRD D1), accepting same-second same-size false hits? Recommend yes.
2. Small-file threshold fixed at 1 MiB in v1 (PRD D2), even though many-small-file folders are still read? Recommend fixed now, configurable later if asked.
3. Deep file digests unprefixed (PRD D3) vs every digest prefixed (file inputs re-run once too)? Recommend unprefixed.
4. Format-change note in 0.9.0 release notes + docs, no CHANGELOG file (PRD D5)? Recommend yes.
5. tc-plugins fix (treat unprefixed folder digests as missing): who and when? Recommend it ships in the same tc-os change that bumps the vendored runtime; tc-os must not bump before.
6. Seed digests on `relocate_output` (PRD Should 1) now or later? Recommend later.
7. Should a map take deep slot digests even when no consumer is deep, so tc-plugins `output_digests` can reuse them (backend Q1)? Recommend no until tc-plugins calls `wf.digest`.
8. Pass an outer loop entry's `cache:` through `lower_loop_entry` (backend Q2)? Recommend no; document "set it in `loop.body`".
9. `fast` in tc-os before tc-os records fast digests and keeps root-input mtimes (PRD Q7, UX Q3)? Recommend document it as not useful there and show Fast disabled with `Not available on Temple Compute yet.` in the editor.
10. horus-ssh: which branch gets `preserve=True` (perf/task-launch, main, or both)? Recommend both; it is three arguments.
11. horus-docs PR (CI `docs-check.yml` requires a linked one): recommend a small PR on running-workflows, `sdk/core/task.mdx` and `sdk/core/workflow.mdx` after WP4, outside these WPs.
12. Editor (UX, doc only): label "Cache check" or "Input check"; show the workflow default in run view; count tasks in the "re-run once" toast; make the PATCH route reject an invalid `cache:`. Recommend "Cache check", per-task display only, yes count, yes reject.
