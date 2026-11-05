# Cache modes: community research

What users report about input hashing and resume caching in workflow engines on large data and shared filesystems, and what breaks a metadata-only (path + size + mtime) cache key. Input for `cache: deep | fast` (see `cache-modes-plan.md`).

Method: web search of GitHub issues, Seqera community, the Snakemake tracker, mailing lists and blog posts, done 2026-11-05. Reddit, HN and Biostars returned almost nothing on this topic. Most of the signal is in GitHub issues. Frequency labels are rough: **common** means many issues or threads and documented by the tool itself, **recurring** means several independent reports, **occasional** means one or two.

Each item is tagged **Fact** (documented behavior or reproduced), **Report** (a user's observation, not confirmed by maintainers) or **Opinion**.

## 1. Top pains

1. **Resume reruns tasks that already finished, on shared filesystems.** Common.
   - Fact: Nextflow's default key is name + size + mtime. Its docs say some shared filesystems (NFS is the one named) report inconsistent timestamps for unchanged files, and they offer `lenient` (name + size only) as the workaround. [cache directive](https://docs.seqera.io/nextflow/reference/process/directives/cache), [cache and resume](https://docs.seqera.io/nextflow/cache-and-resume)
   - Fact: `lenient` came from [nextflow#828](https://github.com/nextflow-io/nextflow/issues/828). With `scratch = true` on SMB3, an input's mtime changed by 1 ms between runs and the task hash changed. The maintainer also saw this "rarely on NFS". He first proposed rounding the timestamp to the second, then shipped `lenient` instead (18.10.1).
   - Report: [nextflow#1629](https://github.com/nextflow-io/nextflow/issues/1629) (PBS, about 10k GATK intervals): resume sometimes reran thousands of finished jobs, which the reporter said could cost "days or weeks" of compute. One user suspected Lustre and said `lenient` helped "somewhat". Another said neither `lenient` nor `deep` fixed it. No maintainer diagnosis; closed by the stale bot. 8 thumbs-up.
2. **Content hashing is slow on large inputs.** Recurring.
   - Report: in nextflow#828, `deep` fixed the false misses but took about 35 s per subject. In [nextflow#1989](https://github.com/nextflow-io/nextflow/issues/1989) the user rejected `deep` because it "slows down my workflow quite substantially as my other inputs are large files".
   - Fact: Cromwell's md5 `file` strategy "works, but uses a lot of resources" ([Cromwell 51 release notes](https://newreleases.io/project/github/broadinstitute/cromwell/release/51)). Cromwell 51 added `xxh64` and `fingerprint` (xxhash of the first 10 MB plus mtime) for shared-filesystem backends ([Janis call-caching notes](https://janis.readthedocs.io/en/latest/references/callcaching.html)).
   - Report, DVC: md5 is the bottleneck on about 400 GB / 29k files ([dvc#3261](https://github.com/treeverse/dvc/issues/3261)); hashes are recomputed during fetch ([dvc#9968](https://github.com/iterative/dvc/issues/9968)); the same file is re-hashed across add/status/push/pull ([dvc#1991](https://github.com/iterative/dvc/issues/1991)); `dvc status` takes 20+ minutes on millions of files ([mirror](https://lightrun.com/answers/iterative-dvc-large-memory-consumption-and-slow-hashing-on-dvc-status-after-dvc-import-of-parent-of-dvc-tracke)); the shared cache is slow on EFS/NFS ([DVC forum](https://discuss.dvc.org/t/shared-cache-on-nfs-is-slow/892)).
   - Report: even on a fully cached resume, Nextflow reads gzipped inputs again, which "becomes a bottleneck ... as our filesystem IO sometimes becomes quite slow" ([Seqera community #1303](https://community.seqera.io/t/is-it-possible-to-recover-cached-processes-without-reading-the-gzipped-files-from-the-disk/1303), no answers).
3. **The same file is hashed many times.** Recurring. This is the pain Horus has today.
   - Fact: Cromwell has `system.file-hash-cache=true` (off by default) to "cache file hashes within the scope of a root workflow to prevent repeatedly requesting the hashes of the same files multiple times" ([Cromwell call caching](https://cromwell.readthedocs.io/en/stable/cromwell_features/CallCaching/)).
   - Fact: DVC keeps a state DB of (inode, mtime, size) → md5 so it can skip re-hashing unchanged files ([DVC state tracking](https://deepwiki.com/treeverse/dvc/3.4-state-tracking)). Users still hit redundant hashing (dvc#1991, dvc#10059).
4. **Users cannot tell why the cache missed.** Recurring.
   - Fact: Nextflow users diff `-dump-hashes` output from two runs. Users in nextflow#1629 found it hard to read.
   - Report: [nextflow#4894](https://github.com/nextflow-io/nextflow/issues/4894) asked for the invalidation reason to be printed to the console. It was closed in favour of lineage `diff`. [nextflow#4344](https://github.com/nextflow-io/nextflow/issues/4344) asked for docs on how invalidation works, because it "changed several times".
5. **Unexpected reruns after an engine changes its rerun rules.** Common for Snakemake.
   - Fact: Snakemake 7.8 added code/params/input/software-env rerun triggers on top of mtime. [snakemake#1694](https://github.com/snakemake/snakemake/issues/1694) has 19 thumbs-up and about 38 comments. Related: [#1677](https://github.com/snakemake/snakemake/issues/1677), [#1818](https://github.com/snakemake/snakemake/issues/1818), [#2192](https://github.com/snakemake/snakemake/issues/2192), and a [PyPSA thread](https://groups.google.com/g/pypsa/c/LFjccjOyDaQ). The workaround everyone gives is `--rerun-triggers mtime`.
   - Opinion, maintainer: "Rather be conservative to ensure that results are guaranteed to be consistent with the current state of the codebase."
   - Opinion, users: a change like this belongs in a major release, and cosmetic edits should not trigger reruns.

## 2. Most-wanted capabilities

| Capability | Evidence | Frequency |
|---|---|---|
| A cheap mode that does not read contents, as an opt-out from strict hashing | Nextflow `lenient`, Snakemake `--rerun-triggers mtime`, Cromwell `path+modtime` / `fingerprint` all exist because users asked for them (nextflow#828, snakemake#1694) | common |
| Hash each file once per run, or reuse a stored hash | Cromwell `file-hash-cache`, DVC state DB, Cromwell `check-sibling-md5` (use a sibling `.md5` file) ([config example](https://gist.github.com/ruchim/568caa82513099b9d58e9cb89cadee26)) | recurring |
| A per-process or per-task cache-mode override | Nextflow `cache` is a process directive. Users set `deep` only where inputs are small (nextflow#1989) | recurring |
| A readable cache-miss reason | nextflow#4894, nextflow#4344, the Seqera troubleshooting [blog](https://seqera.io/blog/troubleshooting-nextflow-resume/) | recurring |
| A cache key that is independent of the staging path | nextflow#1989: `lenient` still missed on AWS Batch because the absolute staging path is part of the key | occasional |
| Combining triggers (mtime and params) | snakemake#1694 | occasional |

## 3. Complaints about existing tools

- **Nextflow.** `lenient` drops mtime but keeps the absolute path, so it still misses when the staging path changes (nextflow#1989). The maintainer's reply was that "Lenient is mainly made for hpc shared file systems". `deep` is too slow for large inputs. Some cache misses are never diagnosed (nextflow#1629). Opinion seen in issues: the resume logic is "very sensitive"; the Nextflow training docs say that touching a file invalidates the cache.
- **Snakemake.** mtime-only checks miss parameter and code changes, while the 7.8+ triggers rerun too much. There is no content-checksum trigger. A user who chains DVC with Snakemake said they prefer checksums but have to `touch` files by hand ([DVC "preserve timestamps" thread](https://lightrun.com/answers/iterative-dvc-preserve-timestamps-during-caching)). After a fresh clone, people fix timestamps with `--touch` or with `touch -d` loops.
- **Cromwell.** The default md5 is expensive. `path` and `path+modtime` only work with soft-link localization and not with containers (Janis notes). The hash cache is off by default.
- **DVC.** Repeated md5 passes, md5 runs single-threaded ([dvc#2073](https://github.com/iterative/dvc/issues/2073)), and the state/index DB is slow on NFS. The usual fix is to move `state.dir` / `index.dir` to local disk.
- **Make.** Opinion: a [widely cited post](https://apenwarr.ca/log/20181113) lists ways `newer-than` comparison fails: clock skew over NFS, coarse granularity, mmap writes, `mv` of an older file over a newer one, deleted inputs, and virtual filesystems with mtime 0. redo's answer is to record (mtime, size, inode, mode, uid, gid) and treat any change in any direction as dirty. Zed adopted the same "`!=`, not `>`" rule ([commit](https://git.secluded.site/zed/commit/14ea4621ab98a315c4742c379723b8dbfd2087b9)).

## 4. Gotchas for a path + size + mtime mode

Horus fast mode compares the recorded (relative path, size, mtime) for equality. It does not check newer-than. That splits the failure modes into two kinds:

- **False miss:** the key changes but the content did not. The task reruns, which costs time but is safe.
- **False hit:** the content changed but the key did not. A stale result is reused, which is unsafe.

### False misses (key changed, content did not)

| Gotcha | Fact / source | Frequency |
|---|---|---|
| GNU tar's default format stores whole seconds. A round trip drops the nanoseconds | Fact, reproduced here with GNU tar 1.35: `.123456789` became `.000000000`. `--format=posix` kept all 9 digits | likely in any tar-based transfer |
| Python `tarfile` (PAX, the default since 3.8) stores mtime as a float, so it keeps sub-second time but not the exact nanoseconds | Fact, reproduced with Python 3.12: `...123456789` came back as `...123456716` | applies whenever Python packs and unpacks |
| `cp` without `-p`/`-a`, and Python `shutil.copyfile`, set mtime to now. `shutil.copy2` keeps the nanoseconds | Fact, reproduced here | common |
| scp `-p` and SFTP v3 carry whole seconds only. Without `-p`, scp sets mtime to now | Fact: [OpenSSH list, 2023](https://lists.mindrot.org/pipermail/openssh-unix-dev/2023-May/040752.html); scp `T<mtime> 0 <atime> 0` record ([SSHD-171](https://issues.apache.org/jira/browse/SSHD-171)) | common |
| rsync needs `-t` (included in `-a`). Its quick check compares whole seconds by default because ext3 zeroes nanoseconds; `--modify-window=-1` compares nanoseconds | Fact: [rsync 3.1.3 commit note](https://lists.samba.org/archive/rsync-cvs/2016-January/007411.html) | common |
| Docker `COPY`/`ADD` and `docker commit` truncate mtime to seconds. `docker save`/`push` do too. Open since 2015 | Fact: [moby#17175](https://github.com/moby/moby/issues/17175) (15 thumbs-up), [moby#23511](https://www.github.com/moby/moby/issues/23511) | recurring |
| S3 cannot set LastModified. An upload always gets the upload time. Mountpoint for S3 cannot set mtime. rclone and s3cmd keep it in their own, different metadata headers | Fact: [aws-cli#2000](https://github.com/aws/aws-cli/issues/2000), [discussion #7071](https://github.com/aws/aws-cli/discussions/7071), [Mountpoint note](https://dev.classmethod.jp/articles/tsnote-mountpoint-for-amazon-s3-timestamp-metadata-retention/), [rclone forum](https://forum.rclone.org/t/last-modified-retention/26014) | common in cloud setups |
| FSx for Lustre imports mtime from `x-amz-meta-file-mtime` when it is present. Without the `ns` suffix the value is read as milliseconds. Objects without that header get S3-derived times | Fact: [FSx POSIX metadata](https://docs.aws.amazon.com/fsx/latest/LustreGuide/posix-metadata-support.html) | relevant for S3-backed FSx |
| `git clone` sets every mtime to checkout time | Fact; [brandur.org](https://brandur.org/fragments/s3-sync), Snakemake clone threads | common |
| A shared filesystem reports a slightly different mtime for an unchanged file (1 ms on SMB3; also seen on NFS) | Report: nextflow#828; Nextflow docs | recurring |
| Precision differs between filesystems: Lustre had 1 s granularity as of 2016 (current state not confirmed); GlusterFS truncated nanoseconds to microseconds; ext3 stores seconds | Fact (dated): [Dilger 2016](https://lists.linaro.org/archives/list/y2038@lists.linaro.org/thread/7NKIMXEWUUIWAK6VCEQMKNNHUAXB4F3O/), [Gluster bug 1419733](https://lists.gluster.org/pipermail/bugs/2017-February/078376.html) | occasional |
| An absolute path in the key misses when the work dir or staging dir moves | Fact: nextflow#1989 | occasional. The Horus plan uses relative paths, which avoids this |

### False hits (content changed, key did not)

| Gotcha | Fact / source | Frequency |
|---|---|---|
| "Racy" write: a file is rewritten in place with the same size within one timestamp tick of when it was recorded | Fact: [git racy-git](https://git-scm.com/docs/racy-git). Git re-reads any entry whose mtime is not older than the index file | rare, but coarse-precision filesystems and fast producers make it more likely |
| Tools that restore mtime on purpose: `touch -r`/`-d`, `cp -p`/`rsync -t` of a different file with the same size, tar extracting an older archive over newer files | Fact; [apenwarr](https://apenwarr.ca/log/20181113) (utimes) | occasional |
| Writes through mmap may not update mtime promptly | Report: apenwarr ("behavior varies by OS") | rare |
| NFS attribute caching: a second client (for example the head node) can see a stale size and mtime for up to `acregmax` (default 60 s) after another node writes, unless the mount uses `noac`. Close-to-open only covers clients that open the file | Fact: [nfs(5) summary](https://gist.github.com/kofemann/2f0e63a1bc00865d1085658715d4fa3e), [IBM AIX NFS attr cache](https://www.ibm.com/docs/ssw_aix_72/performance/nfs_file_attr_cache_tun.html); Snakemake added `--latency-wait` for the related "file not visible yet" problem | recurring on NFS |
| Same size, same mtime, different bytes after a copy: rsync's default quick check skips such files | Fact: rsync docs (`--checksum` / `--ignore-times`) | rare |

### Not a problem for an equality key

- Clock skew between nodes breaks `newer-than` checks in Make and Snakemake ([NetApp KB](https://kb.netapp.com/on-prem/ontap/Ontap_OS/OS-KBs/NFS_file_timestamps_show_future_time), [Quantum ESPRESSO list](https://lists.quantum-espresso.org/pipermail/users/2012-July/024505.html)). An equality key does not care about clock order, as long as the same file keeps the same recorded mtime.

## 5. What this suggests for Horus (input to design, not decisions)

- Keep `deep` as the default. In every tool surveyed, fast/lenient is the opt-in, and the community's main complaint about it is false misses. False hits are rare and come from edge cases.
- Prior art offers two ways to handle precision loss in copies: compare whole seconds (rsync's default, and the "soft" mode first proposed in nextflow#828), or keep nanoseconds and make every copy path preserve them (`tar --format=posix`, `shutil.copy2`, `rsync -a`). Whole seconds widen the racy-write window. Nanoseconds turn every truncating copy (scp, SFTP, Docker COPY, GNU tar default, S3) into a rerun.
- Digest at the source, as the plan already does, so copy-path mtime loss only affects digests taken of staged copies.
- Per-run hash dedup (plan scope item 1) is what Cromwell `file-hash-cache` and the DVC state DB provide, and users ask for it independently of the mode.
- A one-line cache-miss reason (which input, and which of size/mtime/content changed) answers a recurring request.
- On NFS, a fast digest taken on the head node right after a compute node writes may read a stale size or mtime. The result is a later false miss, not a false hit, unless the stale attributes happen to equal those of a later different file.
