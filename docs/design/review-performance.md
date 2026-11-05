# Cache modes: performance review

Read-only review of `git diff cb82b22..HEAD` (2e37142, ae7a20e, 126ecd4) on `cache-modes`. Nothing was run; every number is an estimate from the code and the plan's measured figures (FSx Lustre ~240 MB/s, one head-node client). Plugin code read at horus-slurm 676c12e, horus-ssh-perf 8a8c339, tc-plugins b712a32c.

## Verdict

For the measured topology (Horus on the head node, every Slurm target on the same `location_id` through `SlurmTarget.location_id -> inner.location_id`, horus-slurm `slurm.py:432`), the feature removes the end-of-run hashing. In deep mode each map output byte is read once, during the run, by the slot digests. Nothing is read after the last task. Fast mode reads no big file. Two gaps remain:

- On a first run, a consumer on another location still reads its staged copy, up to once per consumer (H1).
- A map's over input is read twice (M2).

## Q1. rnaseq trace: who reads what, where, when

Shape: maps of 12 slots that write folders; chained maps (`STAR -> markdup -> QC maps`); 18 consumers of the 30 GB markdup and 78 GB STAR folders.

| Read | Code | Where it runs | Deep bytes | Fast bytes | When |
|---|---|---|---|---|---|
| Slot digest per clone, once per consumer mode | `map.py:976`, `:994-1012` | `clone.target.run_command_sync`. On Slurm this is the head node (`slurm.py:464-475`), so 12 run at once there | Each slot once: 30 + 78 = 108 GB in total | Files < 1 MiB only, about 0.1 GB | As each clone finishes, before `stream.ready(slot)` |
| Map root seed | `map.py:891`, `:1014-1053` | One `list_dir`. Every slot digest is a cache hit | 0 | 0 | After `_settle`, before `stream.close` |
| Chained clone item (`B[s]` reads `A[s]`) | `horus_task.py:162-171` via `map.py:793-799` | Same key as the slot digest: same location, same path | 0 (hit) | 0 | First line of `B[s].run` |
| `map.py:906` streamed sources after the chained map settles (the old end-of-run cost) | `base.py:1682` -> `digest` | Hits A's seed (B is an edge target of A, so B's mode is in `_consumer_modes`) | 0 | 0 | After the last clone, before: 0 |
| 18 root consumers, same location (shared FS) | skip check `source_digests`, or first-run `_fingerprint(None)` at the in-place path | Hits the seed | 0 | 0 | — |
| 18 root consumers on another location (SSH host), first run | `_fingerprint(None)` on the staged copy, `horus_task.py:119-122`, `:162-171` | Consumer host | **1x to 18x folder size** (H1) | ~0 | At consumer dispatch |
| Same consumers, unpack | `folder.py:140-141`, package sha | Consumer host | 1 package read (BAM, so close to 108 GB) per unpack call | same | During transfer |
| Over input of the first map (raw FASTQ, ~30 GB) | map `_fingerprint` reads the whole root (`map.py:1407`). Each clone then reads its item (`map.py:803-809`, `digest=None` for the item id) | Head node | 2x input, about 60 GB (M2) | ~0 | Start of the map and of each clone |

Totals for the two big folders:

- **Before:** ~770 GB measured after the last task, plus ~108 GB during the run (each chained clone read its item in `_fingerprint`).
- **After, deep, shared FS:** **108 GB, all during the run. 0 bytes after the last task.** The 108 GB is the read that chained clones already did during the run; it now happens in the producer, before `ready`.
- **After, fast:** about 0.1 GB of small files, plus `stat` calls.
- **Whole run, deep:** about 170-200 GB (108 GB + the FASTQ read twice + other map outputs once). Before, it was about 940 GB.
- **Read after the last task:** nothing in the runtime, if these three hold:
  1. Every reader of a map is a direct edge target. A consumer inside a subworkflow or a loop iteration is not covered (`_consumer_modes`, `map.py:983-992`, deviation 7). It reads the root once at its run.
  2. The map's output is not relocated (deviation 13).
  3. The run is not under tc-os. There, `output_digests` (tc-plugins `side_product_upload.py:449-473`) re-reads every task output deep, uncached, right after the task. That includes each map root: another 108 GB, and the last map's root after the last task.
- **No-op re-run, deep:** a map that skips as a whole seeds nothing. Its 18 consumers then share one read of the root (`base.py:449`), so the whole run reads the data about once (~12 min at 240 MB/s). Before, it read it 18 times. In fast mode the re-run reads about 0.

## Q2. Codebase report risk 8: fixed or open

| Hole | Status | Evidence | Remaining cost |
|---|---|---|---|
| Double hash on stale re-runs (`is_complete(None)`) | **Fixed** | `horus_task.py:285` uses `_staged_digests`. With no staged digests, the `is_complete` read and the `run()` read share one run-cache key | 0 extra |
| Pinned inputs hashed twice | **Fixed** | `base.py:1685-1688` and `horus_task.py:165` both call `wf.digest(task.target, ...)` on the same key | 0 extra |
| Package hash on every unpack | **Open** | `folder.py:140-141`: `s=$($h pkg)` runs before the marker check, on every unpack call | One full read of the compressed package per unpack call at the destination. For BAM folders that is about the folder size: ~108 GB per destination for rnaseq |
| tc-plugins `output_digests` | **Open** (other repo) | `side_product_upload.py:457-466` calls `ArtifactStore(...).digest` directly | One extra deep read of every output, map roots included (~108 GB+ per rnaseq run). It also bypasses the cache, so it does not seed the cache for consumers |
| horus-ssh perf: digest at both ends | **Open** (other repo) | `ssh_transfer.py:216-219` calls `ArtifactStore(source).digest` and `ArtifactStore(destination).digest`, deep and uncached | For each local-to-SSH copy, a full source read plus a full destination read when a copy already exists. Up to 2x the folder per copy. Not used in the head-node Slurm topology |

## Findings (ranked)

### High

**H1. First-run consumers on another location still digest their staged copy, up to once per consumer.**

- `scheduler.py:419-426` sets `_staged_digests` only from `input_digests.result`.
- On a first run, `is_complete_before_transfer` returns at "outputs missing" (`horus_task.py:272-279`) before it awaits the digests, so `result` is `None`.
- `HorusTask.run` then calls `_fingerprint(None)` (`horus_task.py:119-122`). That digests every input on `self.target` at the staged path (`:162-171`).
  - Same location: the staged path is the source path, so the read hits the seed.
  - Other location: the key differs from the seed, so it is a full read on the consumer host.
- `_transfer_input` (`base.py:1533-1538`) forgets the staged path after every cross-location transfer, including joined and skipped ones. A consumer whose transfer finishes after another consumer's digest therefore drops that entry and reads the copy again. Result: between 1x and Nx the folder per consumer location. With 18 consumers of 108 GB, the worst case is ~1.9 TB on the consumer host.
- The same path costs a crossing chained map twice:
  - `A[s]` digests the slot before `ready`, which adds latency on the producer.
  - `B[s]` still reads its staged copy, because the item is not in `clone._staged_digests` (`map.py:1570`).
- The tests miss this because every target in them is on one location.
- **Impact:** none in the measured shared-FS topology. It does apply when the consumers are on SSH or another host, which is in the question's scope.
- **Fix (about 3 lines):** in `_skip_or_run` or `_transfer_and_run`, take the source digests before the transfer, under `leases.pulling(task)`, when the task fingerprints: `if task.outputs: await input_digests()`. Do it before `transfer_artifacts`, so a releasable source is still there. The source keys hit the seeds, so `_staged_digests` is always set and staged copies are never read. The `forget_digests` in `_transfer_input` then becomes a fallback only.

### Medium

**M1. `forget_digests` is a linear scan per dispatch, quadratic for wide maps, and the cost estimate in its comment is low.** `base.py:500-516`, `_nested` at `:2023`.

- Each entry costs about 0.6-1 µs: a key compare, a new `any()` generator, a call to `_nested`, `rstrip`, a concat and two `startswith`. At 10k entries that is about 6-10 ms, not the ~1 ms the comment says.
- Entries:
  - A 1000-slot map has about 1 + 1000 items + 1000 x |modes| slots + |modes| root seeds, so ~2k entries.
  - N consumers add about 0 more, because they share the root key.
- Dispatches: 1000 clones + N. Clones dispatch while the table grows, so a clone scans ~1k entries on average: ~1M checks, about 0.7-1 s of event-loop CPU in total.
- Larger maps:
  - 3 chained maps of 1k slots: ~6k entries and ~3k dispatches, about 6-10 s.
  - 10k slots: ~100M checks, about 1-2 min.
  - 3 chained maps of 10k slots: about 10-20 min of blocked event loop over the run.
- **Fix:** keep it linear but cheap. Per drop path, build `anc = {p, *map(str, PurePosixPath(p).parents)}` and `pre = tuple(p.rstrip("/") + "/" for p in drop)`, then test `key[2] in anc or key[2].startswith(pre)`. That needs no Python call per key and is about 5x faster. Index by location plus a sorted path list with `bisect` only if maps pass 10k slots.

**M2. A map's over input is read twice in deep.**

- The map's `_fingerprint` digests the whole input root (`map.py:1407-1421`).
- Each clone's item source has `digest=None` (`map.py:803-809`), because the item id is not in `_input_digests`. Each clone therefore reads its own item again (`horus_task.py:162-171`).
- For rnaseq that is the raw FASTQ, about +30 GB. Zip inputs behave the same way.
- The Merkle walk already computes every top-level child's digest (`store.py:140-164`).
- **Fix:** have `ArtifactStore.digest` also return the top-level child digests, or add a `_parse_digest` variant that does. In `BaseWorkflow.digest`, seed `root/<name>` for each child. Clone items then hit. The same seeding gives a whole-map skip the slot keys for free.

**M3. No `-z`/`-printf` (busybox, macOS `shasum`) means several forks per file. That is a regression on folders with many files.**

- `store.py:63-76`: when `$h -z` is missing, both `hashes` and `stats` fork `sh`, `$h` and `cut`, or `$st`, for every file.
- At 100k files that is about 400k forks: about 5-15 min. The old command was `find | sort -z | xargs -0 $h` with no fork per file, so it took seconds.
- This affects alpine or busybox Docker images and macOS SSH hosts in both modes.
- **Fix:** batch with `find . -type f -exec $h {} +` and parse lines. Take the per-file loop only for names that contain a newline: `find ... -name "*$nl*"`.

**M4. Fast mode reads every small file and walks the tree twice.**

- `stats && ... hashes -size -1048576c` (`store.py:85-87`) walks the tree twice and stats each file twice. The second walk is mostly served from the client cache.
- It opens and reads every file under 1 MiB.
- 100k small files of ~20 KB on Lustre: 100k open+read+close at ~1-3 ms each, single-threaded, is about 2-5 min, plus 2 GB at 240 MB/s (~8 s). Local NVMe: about 3-5 s.
- So for many-small-file folders, fast costs about the same as deep (open question 2). STAR and markdup slots have about 10 files each, so rnaseq is not affected.
- **Fix:** none needed for v1. Document it. Later options: a single `find` with `-fprint0` for the small set, or `xargs -P`.

### Low

**L1. Parsing and Merkle composition are synchronous on the event loop.**

- `_parse_digest` and `_tree_digest` (`store.py:140-215`) cost about 3-5 µs per file: a regex, a split, nested dicts, and a sort per directory.
- 100k files: 0.3-0.5 s blocked. 1M files: 3-5 s.
- **Fix:** `return await asyncio.to_thread(_parse_digest, mode, out)` at `store.py:264`.

**L2. The whole command output is held in memory, plus its split.**

- `proc.communicate()` (`store.py:260`) holds the output. The deep record is about 66 bytes + path. The fast record is about 2x that for small files.
- 100k files: 13-21 MB of output and about 60-90 MB peak after `split` and the dicts. 10M files: several GB peak. Before, the peak was about 1x the output, because it was hashed whole.
- **Fix:** none for now. If a folder ever gets that large, stream the parse from `stdout`.

**L3. Slot digests run after the clone's lease is finished.**

- `_run_item` digests after `execute_task` returned. By then `member_finished` has run, so a releasable machine can be released mid-digest. The error is caught (`map.py:1006-1012`), consumers then read the relocated copy, and the bytes are wasted. Slurm is not affected.
- **Fix:** digest inside `leases.pulling(clone)`.

**L4. Multiple modes and inputs are read one at a time.**

- `_digest_slot` runs one mode after another (`map.py:1004`). Deep and then fast adds only a stat pass.
- `source_digests` loops over inputs one at a time (`base.py:1682`). This predates the feature, and seeded roots make it moot for rnaseq.
- `_seed_folder_digest` reads a failed slot again, slot by slot (`map.py:1037-1046`), only after a failure.
- **Fix:** none needed. Optionally `asyncio.gather` the per-input digests.

## Q3. Cost of the new machinery

- **`forget_digests`:** see M1. ~2k entries for a 1000-slot map; N consumers add about 0. About 1 ms per dispatch at that size, 6-10 ms at 10k entries. The total grows with dispatches x entries.
- **Slot digest on the critical path:** 6 GB at 240 MB/s is about 25 s for one slot alone (I/O-bound; sha256sum runs at ≥1 GB/s on one core). If 12 slots finish together, they share the client's bandwidth: 72 GB is about 5 min until the last slot is ready.
  - **Chained clone on the same location:** no added delay compared with before. `B[s]` used to read the same slot in its own `_fingerprint` before `_execute` (first run) or in its skip check (re-run). The read moved from consumer to producer.
  - **Crossing chained clone:** added latency plus a second read until H1 is fixed.
  - **Map that only has root consumers:** completion is delayed by its last slot's digest, but the consumers used to wait longer for N root reads.
- **Fast mode, 100k small files:** see M4 for the timing. Output is ~21 MB in memory (L2), and composition takes ~0.3-0.5 s on the loop (L1).
- **Python Merkle cost:** O(n log n), ~3-5 µs per file. It matters only from ~1M files up.

## Q4. Concurrency

- **Different artifacts on one target run at the same time.** `BaseWorkflow.digest` (`base.py:449-480`) has no lock, only one future per key. The 12 slot digests of a map run in parallel on the head node, one command each. That is more parallelism than before, when each root was one single-stream `xargs sha256sum`.
- **Same key:** readers share one future through `asyncio.shield`, and `None` and errors are not kept.
- **SSH session limits:** horus-ssh already handles MaxSessions (`ssh_target.py:63-110`).
- **New serialization on the event loop:** only the synchronous CPU in M1 and L1. No new locks and no new awaits in sequence on the hot path, apart from the per-mode loop in L4.

## Q5. Do the benchmarks measure what they claim?

**Claims:** in `test_digest_cache.py:373-468`, deep goes from 6 x 8 MiB to 8 MiB and fast reads 0 bytes. In `test_map.py:4818-4895`, the map reads 8 MiB from its slots in deep and 0 on the root. Under these setups, both assertions are sound:

- The spy patches `ArtifactStore.digest` at class level, so it sees every caller that goes through it, including direct ones.
- The `horus-tree` substring is in every digest command, file or folder, so the command cross-check counts all of them.
- The byte rule (`< SMALL_FILE` in fast) matches `-size -1048576c`.

What the measurement misses:

1. **Location.** Everything runs on one location (`LocalTarget`, or `fake://cluster` at `test_map.py:4510-4513`). That hides H1: staged-copy reads and the `_transfer_input` forget.
2. **Reads outside `ArtifactStore.digest`.** The unpack package sha (`folder.py:141`) and pack/tar reads are not counted.
3. **File inputs in the plain bench.** `test_digest_cache.py:396` uses `os.walk(root)`, which yields nothing for a file artifact, so a file digest would count 0 bytes. The map helper `_bytes_read` (`test_map.py:4517-4528`) handles files.
4. **What the map bench counts.** It counts only `spy.under(root)`. The map's whole-input read and each clone's item read (M2) are on `batches_in` and are left out on purpose. They are real bytes, though.
5. **The "before" baseline.** It is the new code with the cache bypassed, not 43fc77e. It leaves out the old double hash on stale re-runs and the double hash of pinned inputs, so it understates the old cost. That is conservative.
6. **Bytes by definition.** Bytes are counted from file sizes at call time, not from real I/O. A command that read more than it should (for example fast reading big files) would only be caught by the WP1 chmod-000 test.
7. **No timing, no end-of-run check.** Nothing asserts "no digest after the last task". `test_upstream_dropped_slot_and_chained_settle_read_nothing` (`test_map.py:4704`) covers the chained `:906` path for roots.

**Suggested test for H1:** two locations (a `LocalTarget` subclass with another `location_id`) and 3 consumers. Assert no digest call on the consumers' staged path in the first run.

## Estimated rnaseq bytes read

| | Before | After, deep | After, fast |
|---|---|---|---|
| After the last task | ~770 GB (~1 h) | 0 | 0 |
| The two big folders, whole run | ~880 GB (770 + 108 during the run) | 108 GB, during the run | ~0.1 GB |
| Whole run (rough) | ~940 GB | ~170-200 GB (60 GB of it is M2) | ~0.1-0.5 GB |
| Cross-location consumers, first run (not the measured setup) | 18 x 108 GB staged | 1x to 18x 108 GB until H1 is fixed | ~0 |
| tc-os only: `output_digests` | +108 GB+ | +108 GB+ (open) | +108 GB+ (always deep) |
