# Cache modes: prior art

How other workflow and build tools decide that a task's inputs changed, and what that costs on large data. Input for the `cache: deep | fast` design in `cache-modes-plan.md`. Sources were read on 2026-11-05. Statements marked "(source code)" come from reading the source, not the docs.

## Comparison

| Tool | Default | Modes | Config surface | Directory digest | Digest reuse | Pitfalls | Links |
|------|---------|-------|----------------|------------------|--------------|----------|-------|
| Nextflow | `cache true` (standard): path + size + mtime per input file [1] | `false`; `true` (path, size, mtime); `'lenient'` (path, size); `'deep'` (file content) [1] | `cache` directive per process [1]; in config, the `process` scope with `withName`/`withLabel` selectors overriding the scope default [3]; `process.cache = 'lenient'` used in config [5]; env var `NXF_CACHE_MODE` sets the default mode (source code) [4] | Non-asset directories are hashed by their own path, size and mtime, not walked, in every mode. `deep` only reads regular files (source code) [6] | None across processes: a cache entry belongs to one process, and the task hash includes the session ID and process name [7] | Full path is in the hash, so lenient broke when the staging path changed between runs on AWS Batch [5]. NFS can report inconsistent timestamps, which is what lenient is for [2][1]. `deep` is slow on large inputs [5] and on cloud means pulling the file to hash it [7] | [1] [2] [3] [4] [5] [6] [7] |
| Snakemake | All triggers: `code,input,mtime,params,software-env` [8]. mtime-only before 7.8.0 [11] | `--rerun-triggers` subset; files under `--max-checksum-file-size` (default 1 MB) are compared by checksum, so a newer mtime with the same content does not rerun [9] | Triggers are global on the CLI; no per-rule choice [11]. Per input: `ancient()` ignores its timestamp [10]. `--rerun-incomplete` reruns jobs whose outputs are marked incomplete [8] | `directory()` outputs get a hidden `.snakemake_timestamp` file whose mtime stands for the directory [10] | Metadata kept per output file in `.snakemake` (file or SQLite backend) [8] | Git checkout resets mtimes and causes reruns; the FAQ suggests restoring commit dates or `ancient` [9]. Users report unexpected reruns from software-env and code triggers [11][12] | [8] [9] [10] [11] [12] |
| Cromwell | Call caching off [13]. When on, local/shared FS uses `md5` of content [14] | Local: `md5`, `xxh64`, `path`, `path+modtime`, `fingerprint` (mtime + size + xxh64 of first 10 MB, `fingerprint-size`), plus `check-sibling-md5` (use an existing `<file>.md5`) [14]. Cloud: object metadata only (GCS `crc32c`/`md5`/`identity`, S3 `etag`); Cromwell never downloads to hash [14] | Server config per backend [14]; workflow options `read_from_cache`/`write_to_cache` [13]; per task `meta { volatile: true }` disables caching [15] | Not documented. Cromwell "does not check that the contents of the output directory changed" [13] | `system.file-hash-cache=true` caches file hashes for the root workflow; off by default [13] | `path` modes need soft-link localization; `fingerprint` needs hard links "as copying changes the last modified time" [14]. GCS `md5` is missing for multipart uploads [14] | [13] [14] [15] |
| cwltool / Toil | No reuse unless `--cachedir` is given [16] | Per input file: size + provided `checksum` if present, else size + mtime in ms (source code) [17] | CLI `--cachedir`. Toil `--restart` resumes a failed run from its job store; `--cachedir` support in toil-cwl-runner is a separate effort [18] | Directories get no key entry; only `File` objects are hashed (source code) [17] | Producer-supplied checksums are reused instead of rehashing (source code) [17] | Key uses the resolved path relative to basedir, so moving inputs changes the key (source code) [17] | [16] [17] [18] |
| Bazel / Buck2 / REAPI | Content digest (SHA-256 by default, set by `--digest_function`) [19][22] | One mode; optional in-memory file digest cache keyed by stat data [20][21] | Global flags | Merkle tree: each `Directory` lists children sorted by name, each with the digest of the file blob or child `Directory`; the root digest is the action's `input_root_digest` [22]. A `Digest` is hash + size [22] | Action cache maps the action digest to its result; CAS holds outputs by digest [19]. Buck2 uses the same action-digest model [23] | Caching digests by path, inode, mtime, size gave wrong results when a tar with fixed mtimes was extracted and moved over a file keeping the inode; Bazel 6.1.2 added ctime to the key [20][21]. Tools outside the workspace are not tracked [19] | [19] [20] [21] [22] [23] |
| DVC | md5 of content [24][25] | One mode | `dvc add` / stage outs | `.dir` object: JSON array of `{md5, relpath}` entries; directory hash = md5 of that JSON plus `.dir` suffix [24][26]. Flat listing, not a tree. `.dvc` records `size` and `nfiles` [25] | SQLite state DB stores file hashes with timestamps and inodes to skip rehashing unchanged files [24]; older code also stored size [27] | DVC 2.x file md5 does not always equal `md5sum` of the file [26] | [24] [25] [26] [27] |
| Make | mtime: rebuild a target older than any prerequisite [28] | One mode | Makefile | Directory mtime only | None | Any copy or checkout that changes mtimes triggers rebuilds | [28] |

## Notes per question

- **Default mode.** Engines built for big data default to metadata (Nextflow, cwltool, Make, Snakemake's mtime check above 1 MB) [1][17][28][9]. Cromwell defaults to content md5 but ships with caching off [13][14]. Build systems (Bazel, DVC) default to content and lean on a stat-keyed digest cache to stay fast [20][24].
- **Per-task vs global.** Nextflow is the only one with a per-process mode plus a config-level default and selector overrides [1][3]. Cromwell picks the strategy per backend on the server and only offers per-task on/off (`volatile`) [14][15]. Snakemake triggers are global [11].
- **Directory digest structure.** REAPI uses a Merkle tree [22]; DVC uses one flat manifest [24][26]; Nextflow hashes repo-asset directories by summing per-entry hashes in any order, and other directories not at all (source code) [6].
- **Digest reuse across consumers.** Cromwell's root-workflow file hash cache [13] and Bazel's CAS/action cache [19] are the only documented cross-consumer reuse. Nextflow maintainers list per-file output hashes as future work [7].
- **Copies that drop mtime.** Cromwell documents that copying breaks `fingerprint` and requires hard links [14]. Snakemake documents checkout resetting mtimes [9]. Nextflow's answer to unstable timestamps was a mode without mtime (`lenient`) [1][2].

## Borrow

1. Workflow-level default plus per-task override, Nextflow style [1][3]. Matches the planned `cache:` on `horus_workflow` and tasks.
2. Merkle composition from REAPI [22]: directory digest from sorted `(name, child digest)`. This is what lets a map digest be built from per-slot digests without rereading.
3. A run-scoped digest cache shared by all consumers, as Cromwell's `file-hash-cache` [13], but on by default.
4. Record the digest where the data is produced and reuse it downstream: Cromwell `check-sibling-md5` [14], cwltool's producer checksums [17], and the stage-out hash idea in Nextflow discussion #5612 [7]. Same idea as per-clone slot digests.
5. Size in every digest entry (REAPI `Digest` = hash + size [22]); cheap and catches truncation in fast mode.
6. A hash dump for debugging misses, like Nextflow `-dump-hashes` [2][29].
7. Later, not v1: a persistent stat-keyed cache so `deep` does not reread unchanged files across runs (DVC state DB [24], Bazel digest cache [20]). Key it on inode + size + mtime + ctime, per Bazel's fix [21].

## Avoid

1. Absolute paths in the key (Nextflow #1989 [5], cwltool basedir-relative path [17]). Keep digests path-independent, as Horus does today.
2. Hashing a directory by its own stat only (Nextflow non-deep [6], Make [28]). A directory's mtime changes only when entries are added, removed or renamed [10], so in-place edits are missed. `fast` must walk entries.
3. Dropping mtime entirely (Nextflow `lenient`): same-size edits are missed [7]. If NFS/Lustre timestamps are unstable, prefer `deep` for that task over a weaker fast mode.
4. Partial-content fingerprints (Cromwell `fingerprint` [14]): extra read cost and still mtime-dependent.
5. Global-only switches (Snakemake [11]); users cannot target the one task with large inputs.
6. Treating object-store metadata hashes as content hashes without checking the upload path (GCS `md5` missing on multipart [14]). Relevant if a future target digests on S3.

## Open questions

- Nextflow `deep` on a directory input: the source [6] shows directories fall back to path + size + mtime even in `deep`. No doc or issue confirming this was found; a small Nextflow run would settle it.
- Cromwell: how WDL `Directory` inputs are hashed for call caching is not documented [13][14].
- Toil: whether toil-cwl-runner's `--cachedir` support from PR 5187 is merged and how it keys inputs [18].
- DVC: whether the current state DB still stores size, or only inode + mtime [24][27].
- Buck2: no public description of its directory digest found beyond the action-digest glossary entry [23].
- Snakemake: whether the mtime trigger for `directory()` outputs ever looks at contents, or only `.snakemake_timestamp` [10].

## Links

1. Nextflow `cache` directive: https://docs.seqera.io/nextflow/reference/process/directives/cache
2. Nextflow caching and resuming: https://docs.seqera.io/nextflow/cache-and-resume
3. Nextflow configuration, process selectors: https://docs.seqera.io/nextflow/config
4. Nextflow `CacheHelper.java` (`NXF_CACHE_MODE`): https://github.com/nextflow-io/nextflow/blob/master/modules/nf-commons/src/main/nextflow/util/CacheHelper.java
5. Nextflow issue #1989, lenient on AWS Batch: https://github.com/nextflow-io/nextflow/issues/1989
6. Nextflow `HashBuilder.java`: https://github.com/nextflow-io/nextflow/blob/master/modules/nf-commons/src/main/nextflow/util/HashBuilder.java
7. Nextflow discussion #5612, deep cache cost on cloud: https://github.com/nextflow-io/nextflow/discussions/5612
8. Snakemake CLI: https://snakemake.readthedocs.io/en/stable/executing/cli.html
9. Snakemake FAQ: https://snakemake.readthedocs.io/en/stable/project_info/faq.html
10. Snakemake rules (`directory()`, `ancient`): https://snakemake.readthedocs.io/en/stable/snakefiles/rules.html
11. Snakemake issue #1677, 7.8 triggers: https://github.com/snakemake/snakemake/issues/1677
12. Snakemake issue #1694, code trigger: https://github.com/snakemake/snakemake/issues/1694
13. Cromwell call caching: https://cromwell.readthedocs.io/en/latest/cromwell_features/CallCaching/
14. Cromwell configuring (hashing strategies): https://cromwell.readthedocs.io/en/stable/Configuring/
15. Cromwell volatile tasks: https://cromwell.readthedocs.io/en/stable/optimizations/VolatileTasks
16. cwltool CLI: https://cwltool.readthedocs.io/en/latest/cli.html
17. cwltool `command_line_tool.py` cache key: https://github.com/common-workflow-language/cwltool/blob/main/cwltool/command_line_tool.py
18. Toil PR #5187, cache-aware toil-cwl-runner: https://github.com/DataBiosphere/toil/pull/5187
19. Bazel remote caching: https://bazel.build/remote/caching
20. bazel-discuss, how Bazel knows a file changed: https://groups.google.com/g/bazel-discuss/c/6VmjSOLySnY/m/v2dpwt8jBgAJ
21. Bazel commit, ctime in file digest cache key (#18101): https://bazel.googlesource.com/bazel/+/refs/heads/release-6.1.2%5E%21/
22. Remote Execution API proto: https://github.com/bazelbuild/remote-apis/blob/main/build/bazel/remote/execution/v2/remote_execution.proto
23. Buck2 glossary (action digest): https://buck2.build/docs/concepts/glossary
24. DVC internal files: https://doc.dvc.org/user-guide/project-structure/internal-files
25. DVC `.dvc` files: https://doc.dvc.org/user-guide/project-structure/dvc-files
26. DVC discussion #9732, `.dir` hash: https://github.com/iterative/dvc/discussions/9732
27. DVC 0.30.1 `state.py`: https://grok.dragonflybsd.org/xref/dports/science/dvc/dvc-0.30.1/dvc/state.py
28. GNU make man page: https://manpages.ubuntu.com/manpages/focal/man1/make.1.html
29. Troubleshooting Nextflow resume: https://nextflow.io/blog/2019/troubleshooting-nextflow-resume.html
