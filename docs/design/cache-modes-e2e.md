# Cache modes: E2E report

Run on 2026-11-05 after the local bench finished. Branch `cache-modes` at
541957a; baseline is `map-chaining` 43fc77e. Linux, GNU coreutils and
findutils, local SSD. horus-ssh: branch `cache-modes` at dce1760.

Result: pass. Second runs skip everything in both modes, and an edit re-runs
only the affected chain. Deep reads 2.5x to 4x fewer bytes than the
baseline, fast reads 39 bytes per run, and the SSH round trip keeps mtimes.
One finding (F1): fast misses a change when a producer re-runs inside the
same second as its previous write.

## Workflow

`horus_workflow` with local targets only, run from the YAML as
`horus run --no-tui` does: `BaseWorkflow.from_yaml` then `wf.run`.

- Root folder `raw`: 4 files of 4 MiB. Root folder `ref`: one 4 MiB file and
  one 7-byte file.
- `proc`: map over `raw`; each clone copies its sample into its slot of
  `out/processed`, so the output is 4 slots of 4 MiB.
- `post`: chained map (`chain: true`) over `out/processed`; writes `size.txt`
  per slot into `out/sizes`.
- `c1`, `c2`, `c3`: consumers of the whole `out/processed` folder.
- `report` consumes `out/sizes`, `rawlist` consumes `raw` and `refidx`
  consumes `ref`.

Deep and fast runs use separate directories with the same data layout
(`cache: deep` or `cache: fast` at the workflow level). The baseline runs the
same YAML without `cache:` on 43fc77e's `src`.

## Measuring

- **Digest commands:** counts every `run_command_sync` call that carries a
  digest script (`horus-tree` on the branch, `horus-dir` on the baseline),
  and logs its path.
- **Bytes read:** a `sha256sum` shim first on `PATH` logs the size of every
  file or stdin it hashes, then runs the real tool. The local runs do no
  packaging, so every hashed byte here is a digest read. The shim also counts
  the 0-byte `-z` probe in the digest prelude, but that adds no bytes.

Wall times are 0.05–0.16 s per run on this data and are not compared.

## Local runs

| Run | Baseline 43fc77e (deep) | `cache-modes`, deep | `cache-modes`, fast |
|---|---|---|---|
| 1. first run | 16 commands, 132 MiB | 14 commands, 52 MiB | 14 commands, 39 B |
| 2. same inputs | 8 commands, 100 MiB | 4 commands, 36 MiB | 4 commands, 39 B |
| 3. after one edit | 23 commands, 220 MiB | 14 commands, 52 MiB | 14 commands, 39 B |
| 4. same inputs | 8 commands, 100 MiB | 4 commands, 36 MiB | 4 commands, 39 B |

### Runs 2 and 4: every task skipped (all three columns)

Only `start` ran; it has no outputs, so it always runs. Every other task
ended `skipped/complete`.

- Baseline: `raw` read twice, by `proc` and `rawlist`. `out/processed` read
  4 times, by `post`, `c1`, `c2` and `c3`.
- Branch: each of `raw`, `ref`, `out/processed` and `out/sizes` is read
  once. A skipped map has no slot digests, so its consumers share one read of
  the whole folder.

### Run 3: one edit

- **Deep:** new content in `raw/s2.bin`, with the same size and mtime
  (`touch -d @<old mtime>`).
- **Fast:** new content in `raw/s1.bin`, mtime moved 10 s later.

Ran: `proc[2]`, `post[2]`, `c1`, `c2`, `c3` and `rawlist` (fast: `proc[1]`,
`post[1]`, ...). The `proc` and `post` map nodes re-expand.

Skipped:
- the other 3 `proc` clones and the other 3 `post` clones
- `refidx`
- `report`: the rewritten slot has the same size, so `out/sizes` is
  unchanged.

The baseline re-ran the same set. Deep caught a content change that a
size+mtime check cannot see.

### Fast-only checks

- **Same size and mtime, new content** in `raw/s3.bin`: nothing re-ran.
  This is the documented false hit.
- **Small file** `ref/meta.txt` (7 B) rewritten with the same mtime:
  `refidx` re-ran. Files under 1 MiB are hashed by content.

### Where the remaining deep bytes go

- **Run 1, 52 MiB:**

  | Read | Size |
  |---|---|
  | `raw` folder | 16 MiB |
  | `raw` again, each clone digesting its item (P3) | 16 MiB |
  | `ref` | 4 MiB |
  | slot digests of `out/processed` | 16 MiB |

  `c1`–`c3`, `post` and the chained map's manifest read no part of
  `out/processed` beyond those slot digests.
- **Run 3, 52 MiB:** the same reads. A re-expanded map's skipped clones still
  digest their item and their slot, 32 MiB here.

## SSH round trip (horus-ssh)

Rig:
- An Alpine sshd container in its own compose project (`cachemodes-e2e`) on
  127.0.0.1:2290, removed afterwards.
- A scratch virtualenv with editable installs of `horus-runtime/cache-modes`
  and `horus-ssh/cache-modes`.
- None of the user's stacks were touched.

Workflow, `cache: fast`:
- Local root folder `inp`: a 4 MiB file, a small file and a 0444 file.
- Local root file `big.bin`: 4 MiB, mode 0444.
- Every source file has mtime 2026-01-01 12:00:00.
- Task `remote` on the SSH target copies both inputs into output folder
  `res`. Task `back` on the local target stats the copy that comes back.

| Run | Result | Digest commands | Bytes hashed locally |
|---|---|---|---|
| 1 | all ran | 3 | 9 B digests + 8 MiB unpack of `res` |
| 2 | `remote` and `back` skipped | 3 | 9 B |
| 3, 5 | forced (`skip_if_complete: false`), all ran | 3 | 9 B + 8 MiB unpack |
| 4, 6 | `remote` and `back` skipped | 3 | 9 B |

The 8 MiB is `FolderArtifact.unpack_command` hashing the returned package. It
is a known cost and not an input digest.

**mtimes:**
- Staged on the remote: `big.bin` (SFTP `put` plus `utime`) and the `inp`
  files (tar) keep 1767265200.
- Back on local: every copied file keeps 1767265200.

**Modes:**
- The SFTP-put `big.bin` arrives 0644, not 0444, and the forced re-put over
  it succeeds.
- Tar copies keep modes (`ro.txt` stays 0444), as before the change.
- Run 3 first failed in the test's own `cp -p` over its previous 0444
  output. Changing it to `cp -pf` fixed it; the runtime was not at fault.

**Cross-host digests:** `ArtifactStore.digest` taken on both hosts, in fast
and deep mode, is identical for each pair:
- local `inp` vs its remote staged copy
- local `big.bin` vs its remote copy
- remote `res` vs its local copy

The SSH→SSH scp path (`preserve=True`) was not exercised. It needs two SSH
targets with different location ids.

## Findings

- **F1, fast false hit on a re-run producer (medium).**
  - Seen in a fast run 3: `proc[1]` re-ran and rewrote
    `out/processed/1/data.bin` with new content of the same size. It did so
    in the same wall-clock second as run 1's write (19:14:20 for both), so
    the slot's fast digest did not change and `post[1]`, `c1`, `c2` and `c3`
    skipped with stale inputs.
  - Repeating the edit a minute later re-ran the full chain.
  - This is the documented "same size in the same second" limit, but here
    the runtime triggered it, not the user.
  - Real tasks rarely finish within a second of their previous write. Quick
    tasks in tests or CI can.
  - Possible fixes:
    - in fast mode, treat a task-produced input as changed whenever its
      producer ran in this run;
    - or compare sub-second mtimes for outputs that stay on the producer's
      machine (copies made with tar or scp keep whole seconds only).
  - Not fixed here.
- **F2 (known, P3): map items are read twice in deep mode,** once in the
  folder digest and once per clone. That is 16 of the 52 MiB in run 1.
- **F3: a re-expanded map re-digests every item and slot,** even for clones
  that skip (32 MiB in deep run 3).
- **F4 (known): folder unpack hashes the package** on every transfer
  (8 MiB per SSH run 1).

## Reproduce

The scripts are in the session scratchpad and are not committed:
- `driver.py`: one run, counts commands, reads the shim log.
- `shim/sha256sum`
- `wf.yaml.in`
- `ssh/wf.yaml`
- `crosscheck.py`
- `sshd/`: Dockerfile and compose file.

They can be added under `tests/` if wanted.
