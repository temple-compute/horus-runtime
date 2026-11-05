# 0.9.0 release notes: cache modes

## New: `cache: deep | fast`

Workflows and tasks take a `cache` field that sets how input digests are taken
for the skip check.

- `deep` (default): hashes every byte of every input, as before.
- `fast`: hashes each file's path, size and mtime in whole seconds, plus the
  content of files under 1 MiB. Large inputs are not read.

Set it on the workflow for every task, or on a task to override. Map clones
use the map's value. For a subworkflow, set it inside `sub:`; for a loop,
inside `loop.body`.

`fast` misses a same-size rewrite within the same second, and re-runs a task
when a copy or `touch` changes the mtime. Local copies made by the runtime now
keep mtime.

Each input is now digested once per run instead of once per consumer, and a
map composes its folder digest from the slots its clones already digested.

## Upgrading

Folder digests changed format. After upgrading, every task with a folder
input re-runs once, including chained maps. Tasks with only file inputs do not
re-run. Switching a task between `deep` and `fast` also re-runs it once.
