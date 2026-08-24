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

`kind: horus_map` runs its OWN body once per item of a collection
(`src/horus_builtin/workflow/map.py`). It is an ordinary DAG node: edges wire
its inputs/outputs like any other task's.

```yaml
- kind: horus_map
  id: dock                # required; unique; the DAG node key
  name: Dock every ligand # required; human-readable
  over:                   # required; what to iterate and what to call each item
    input_id: ligands     #   id of THIS task's own input holding the collection
    as: ligand            #   id the per-item artifact is created under
  max_concurrency: 2      # optional; upper bound on clones dispatched at once
  inputs:
    - {kind: folder, id: ligands, path: ligands_in}   # the `over` collection...
    - {kind: file, id: receptor, path: rec.pdbqt}     # ...plus shared inputs
  outputs:
    - {kind: folder, id: complexes, path: complexes}  # required; exactly one folder
  runtime:                # the per-item body, run once per item
    kind: command
    command: "dock $ligand $receptor > $complexes/complex.pdb"
  executor: {kind: shell}
```

Iteration contract: the `over.input_id` input must be an `IterableArtifact`
(`core/artifact/iterable.py`), and `items()` returns real artifacts. `folder`
iterates its children sorted by name (each child's own on-target path, zero
copy); `json` iterates its parsed list, writing one single-element JSON
artifact per index into a `<stem>.items` directory next to the parent.

Each clone carries every input the map declares, plus ONE new artifact under
`over.as` holding its item, and the folder output rebound to that clone's own
slot directory. So the body is written as if it handled a single element while
the collection stays addressable: `$ligand` is one ligand, `$ligands` is the
whole collection, `$complexes` is this clone's directory. `over.as` must not
collide with a declared input or output id (validated at load). Slots are
zero-padded indices; clone ids are `<id>[<slot>]`, registered in the DAG via
`expand()` and ordered after the map by an artifact-less edge. Clones inherit
the map's runtime/executor/target/resources (deep-copied) and run as plain
`horus_task`s.

The map only fans out. Producing the collection is whatever upstream task
writes the iterable artifact; folding the slots back into one value is
whatever downstream task consumes the folder output.

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
