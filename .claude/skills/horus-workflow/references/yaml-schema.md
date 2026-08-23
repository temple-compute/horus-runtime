# YAML workflow schema

A YAML workflow is validated with Pydantic through `BaseWorkflow.from_yaml`. Every
nested object is dispatched to a concrete plugin by its `kind` field, so the
schema is open: any registered plugin `kind` is valid wherever its base type is
expected.

## Top level (workflow)

```yaml
name: my_workflow          # required, non-empty, matches ^[a-zA-Z0-9 _-]+$
kind: horus_workflow       # required; selects the concrete workflow plugin
tasks: [...]               # list of tasks (see below)
artifacts: [...]           # optional root artifacts (no producer task)
edges: [...]               # optional edges (the DAG; see below)
```

`orchestrator_target` defaults to `local` for `horus_workflow`; set it only when
dispatching to remote targets that can't reach local files. `id` and `status` are
managed by the runtime — don't set them.

## Task

```yaml
- id: producer            # required; unique; the DAG node key (edges reference it)
  name: Produce data      # required; human-readable
  kind: horus_task        # required; concrete task plugin
  description: ""          # optional
  skip_if_complete: true  # optional (default true): skip if all outputs exist
  runtime:                # required; WHAT to run
    kind: command
    command: "echo hello > $result"
  executor:               # required; HOW to run it (must accept the runtime)
    kind: shell
  target:                 # optional; WHERE (defaults to {kind: local})
    kind: local
  resources:              # optional; advisory (see below)
    cpus: 4
  inputs: [...]           # optional input artifacts
  outputs:                # optional output artifacts (drive skip/completion)
    - id: result
      kind: file
      path: /tmp/out.txt
```

Compatibility is checked at load: the `executor` must list the `runtime`'s type in
its `runtimes` (e.g. `shell` ↔ `command`), else `IncompatibleRuntimeError`.

## Map task

`kind: horus_map` wraps another task and runs one clone of it per item of a
collection (`src/horus_builtin/workflow/map.py`). It is an ordinary DAG node:
edges wire its inputs/outputs like any other task's.

```yaml
- kind: horus_map
  id: dock                # required; unique; the DAG node key
  name: Dock every ligand # required; human-readable
  over: ligands           # required; id of THIS task's own input holding the collection
  item_input: ligand      # required; id of the input on `task` receiving each item
  max_concurrency: 2      # optional; upper bound on clones dispatched at once
  inputs:
    - {kind: folder, id: ligands, path: ligands_in}   # the `over` collection...
    - {kind: file, id: receptor, path: rec.pdbqt}     # ...plus shared inputs
  outputs:
    - {kind: folder, id: complexes, path: complexes}
  task:                   # required; the wrapped, per-clone task (full task shape)
    kind: horus_task
    inputs:
      - {kind: file, id: ligand, path: lig.pdbqt}
    outputs:
      - {kind: file, id: complex, path: complex.pdb}
    runtime: {...}
    executor: {...}
```

Iteration contract: the `over` input must be an `IterableArtifact`
(`core/artifact/iterable.py`). `folder` iterates its children sorted by name
(slot = child name, zero copy); `json` iterates its parsed list (slot =
zero-padded index). Enumeration reads strictly through the target's channels,
so remote targets work. Clone ids are `<id>[<slot>]`; every clone shares each
non-item input verbatim and re-roots its relative output paths under the
folder output. The wrapped task's other inputs are adopted as ports of the
map itself unless re-declared there.

### Fan-out transform

Optional `fan_out:` declares the iterable intermediate the map produces
itself:

```yaml
  fan_out: {kind: json, id: batches, path: batches.json}
  runtime:                # the map's OWN body; runs FIRST, only when fan_out is set
    kind: command
    command: "... > $batches"
  executor: {kind: shell}
```

When `fan_out` is set, the map's `runtime`/`executor` execute before iteration
and their output (the declared iterable intermediate) is what gets iterated;
use this when the `over` input is not itself iterable. When absent,
`runtime`/`executor` never run and `over` must be iterable. The intermediate's
id must not collide with any input or output id.

### Gather transform

Without `gather`, the map needs exactly one `folder` output and fans every
clone's outputs into it. Optional `gather:` declares a second transform that
instead folds the clones' outputs into ONE single output of ANY kind:

```yaml
  outputs:
    - {kind: file, id: merged, path: merged.txt}
  gather:
    kind: command
    command: "cat $slots/*/result.txt > $merged"
```

With `gather`, the clones' outputs are re-rooted into an internal `slots`
folder and the SAME `executor` runs `gather` afterwards to fold `$slots`
(plus any of the map's shared inputs) into the declared output. No input may
use the reserved id `slots` while `gather` is set.

### Transform-executor compatibility

ONE shared `executor` serves the body AND both transforms: every involved
transform runtime (`runtime`, `gather`) must be listed in that `executor`'s
`runtimes`. This is validated at load.

## Artifact

Artifacts appear in a task's `inputs`/`outputs` or as workflow-level root
`artifacts`. Fields:

```yaml
- id: result             # required; stable id (edge endpoint key)
  kind: file             # file | folder | json | pickle | <plugin kind>
  path: /tmp/out.txt     # required; absolute local path (resolved)
  name: ""               # optional; defaults to id
  description: ""         # optional
```

Existence + SHA-256 hash of the `path` decides task completion.

## Edges

Edges wire a producer's output to a consumer's input. They are the sole DAG
source of truth and determine transfer sources.

```yaml
edges:
  - source: producer         # producer task id
    source_output: result    # output artifact id on that task
    target: consumer          # consumer task id
    target_input: infile     # input artifact id on the consumer
```

Rules (validated at load): `target`/`target_input` must be a real task + declared
input; `source`/`source_output` must be a real task + declared output; at most one
edge may feed a given `(target, target_input)`.

**Root-artifact source:** to feed a consumer input from a workflow-level root
artifact instead of a task, use the `artifact-<rootId>` convention:

```yaml
artifacts:
  - id: dataset
    kind: file
    path: /data/input.csv
edges:
  - source: artifact-dataset   # "artifact-" + root id
    source_output: dataset     # the root artifact id
    target: consumer
    target_input: infile
```

## Resources (advisory)

`resources` is optional and consumed only by resource-aware targets; `local`
ignores it. Unknown keys are rejected (`extra="forbid"`).

```yaml
resources:
  cpus: 4          # int >= 1, or omit to let the target choose
  gpus: 1          # int >= 0, default 0
  memory_gb: 16    # int >= 1
  vram_gb: 24      # int >= 1
  walltime: "01:30:00"   # target-interpreted string
```

## Command substitution

`command` (and the script/python string runtimes) render `string.Template`
`$`/`${}` placeholders against the task before running. `str.format` `{}` passes
through untouched. Forms:

- `$id` / `${id}` — the artifact with that id, rendered as its path **on the
  task's target** (so a command written once runs unchanged local or remote).
- `${id.attr}` — an attribute of that artifact (e.g. `${result.path}`).
- `${task.attr}` — an attribute of the task (e.g. `${task.name}`, `${task.id}`).
- `$$` — a literal `$`. Unknown `$name` is left as-is.

The artifact id `task` is reserved and raises `ValueError`.

```yaml
runtime:
  kind: command
  command: "sort ${infile} > $sorted"   # infile is an input id, sorted an output id
```

## Complete examples

### Single task

```yaml
name: hello
kind: horus_workflow
tasks:
  - id: greet
    name: Greet
    kind: horus_task
    runtime:
      kind: command
      command: "echo hello"
    executor:
      kind: shell
```

Run: `horus run hello.yaml` (trigger defaults to `greet`).

### Producer → consumer with an edge, outputs, and skip

```yaml
name: producer_consumer
kind: horus_workflow
tasks:
  - id: producer
    name: Produce
    kind: horus_task
    skip_if_complete: true
    runtime:
      kind: command
      command: "echo 42 > $data"
    executor:
      kind: shell
    outputs:
      - id: data
        kind: file
        path: /tmp/horus_demo/data.txt
  - id: consumer
    name: Consume
    kind: horus_task
    runtime:
      kind: command
      command: "cat ${data_in} > $summary"
    executor:
      kind: shell
    inputs:
      - id: data_in
        kind: file
        path: /tmp/horus_demo/data.txt
    outputs:
      - id: summary
        kind: file
        path: /tmp/horus_demo/summary.txt
edges:
  - source: producer
    source_output: data
    target: consumer
    target_input: data_in
```

Run: `horus run producer_consumer.yaml --trigger producer`. On a second run both
tasks are skipped because their outputs already exist; delete the outputs (or set
`skip_if_complete: false`) to force re-execution.
