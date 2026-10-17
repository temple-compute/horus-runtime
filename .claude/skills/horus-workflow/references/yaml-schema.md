# YAML workflow schema

A YAML workflow is validated with Pydantic through `BaseWorkflow.from_yaml`. Every
nested object is dispatched to a concrete plugin by its `kind` field, so the
schema is open: any registered plugin `kind` is valid wherever its base type is
expected.

## Top level (workflow)

```yaml
name: my_workflow          # required; any non-empty text without control chars
kind: horus_workflow       # required; selects the concrete workflow plugin
targets: {...}             # optional named targets shared by tasks (see below)
tasks: [...]               # list of tasks (see below)
artifacts: [...]           # optional root artifacts (no producer task)
edges: [...]               # optional edges (the DAG; see below)
max_concurrency: 4         # optional; tasks dispatched at once. unset = 8, 0 = no limit
failure_policy: fail_fast  # optional; fail_fast (default) | continue
capacity:                  # optional; per-location resource caps (see Resources)
  "local://myhost": {cpus: 16, gpus: 2}
```

- `failure_policy: fail_fast` cancels every in-flight task on the first
  failure. `continue` lets unrelated branches finish; the failed task's
  descendants end `SKIPPED` (`upstream_failed`) and the workflow still ends
  `FAILED`.
- `orchestrator_target` defaults to `local` for `horus_workflow`; set it only
  when dispatching to remote targets that can't reach local files. It does not
  take a `{ref}`.
- `id` and `status` are managed by the runtime — don't set them.

## Task

```yaml
- id: producer            # required; unique; cannot start with "@"; the DAG node key
  name: Produce data      # required; human-readable
  kind: horus_task        # required; concrete task plugin
  description: ""          # optional
  skip_if_complete: true  # optional (default true): skip if outputs exist and the manifest matches
  runtime:                # required; WHAT to run
    kind: command
    command: "echo hello > $result"
  executor:               # required; HOW to run it (must accept the runtime)
    kind: shell
  target:                 # optional; WHERE (defaults to {kind: local}),
    kind: local           #   or {ref: <name>} for a shared target
  resources:              # optional; see Resources
    cpus: 4
  inputs: [...]           # optional input artifacts
  outputs:                # optional output artifacts (drive skip/completion)
    - id: result
      kind: file
      path: out/result.txt  # relative: anchored under the run directory
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
  max_concurrency: 2      # optional; clones at once. unset = 8, 0 = all at once
  on_item_failure: fail   # optional; fail (default) | drop
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
zero-padded indices (a map over another map's output keeps that map's slot
names); clone ids are `<id>[<slot>]`, registered in the DAG via `expand()` and
ordered after the map by an artifact-less edge. Clones copy the map's
runtime/executor/resources and run as plain `horus_task`s on an `idle_copy()`
of the map's target, so they share the map's machine. The map itself reserves
no resources while it waits; its clones do.

`on_item_failure: drop` removes a failed clone's slot from the output folder
and completes the map, unless every clone failed. The clone stays `FAILED`.
Only use it when items are independent. Under `failure_policy: continue` or
`drop`, one failing clone does not cancel the others.

The map only fans out. Producing the collection is whatever upstream task
writes the iterable artifact; folding the slots back into one value is
whatever downstream task consumes the folder output.

## Artifact

Artifacts appear in a task's `inputs`/`outputs` or as workflow-level root
`artifacts`. Fields:

```yaml
- id: result             # required; stable id (edge endpoint key)
  kind: file             # file | folder | json | pickle | number | boolean | string | choice | <plugin kind>
  path: out/result.txt   # required; absolute, or relative (see Paths)
  name: ""               # optional; defaults to id
  description: ""         # optional
```

Output existence plus the task manifest (input digests + config hash) decides
completion.

### Paths

Relative paths are allowed and anchored at run start: a path some task
produces goes under the run directory, any other path under the base
directory. The base directory is the YAML's folder (process CWD for a
Python-built workflow); the run directory is the base joined with the
orchestrator target's `working_directory`, which is the base itself by
default. Absolute paths are left as they are. Each task runs in
`<target working dir>/<task_id>/<execution id>/`.

### Value artifacts

`number`, `boolean`, `string` and `choice` carry an inline `value`. `$id`
renders the **value**, not a path; the value is also written to `path` the
first time a task consumes it; read that file with `${id.path}`.

```yaml
artifacts:
  - {id: steps, kind: number, path: steps.json, value: 1000}
  - {id: label, kind: string, path: label.txt, value: run-a}
  - id: device                # boolean mapped to the values the task receives
    kind: boolean
    path: device.json
    value: true               # true/false is translated to on/off
    "on": {value: gpu, label: GPU}   # quote: bare on/off parse as YAML booleans
    "off": {value: cpu}
  - id: solvent               # dropdown; value defaults to the first option
    kind: choice
    path: solvent.txt
    options:
      - {value: water}
      - {value: dmso, label: DMSO}
    value: dmso
```

- `boolean`: map both `on` and `off` or neither; mapped values must differ
  and cannot be `true`/`false`. `$id` renders `true`/`false` when unmapped.
  Quote the `"on"`/`"off"` keys: unquoted, PyYAML reads them as `True`/`False`
  and the mapping is silently dropped.
- `choice`: `options` must be non-empty with distinct values; `value` must be
  one of them.
- A consumer input fed by an edge from a value artifact takes the source's
  value.

## Edges

Edges wire a producer's output to a consumer's input. They are the sole DAG
source of truth and determine transfer sources.

```yaml
edges:
  - source: producer         # producer task id
    source_output: result    # output artifact id on that task
    target: consumer          # consumer task id
    target_input: infile     # input artifact id on the consumer
    transfer: true           # optional (default true); false = ordering only
    condition: {...}         # optional; gates the edge (see below)
```

Rules (validated at load):

- `target` must be a real task; `target_input`, when given, a declared input.
- `source` must be a real task (or `artifact-<rootId>`); `source_output`, when
  given, a declared output.
- `source_output` and `target_input` go together. Give both, or neither for an
  ordering-only edge (then `transfer` is forced to `false`). One alone raises
  `IncompleteEdgeError`.
- At most one `transfer: true` edge may feed a given `(target, target_input)`
  (`DuplicateEdgeTargetError`). Ordering-only edges are exempt.

```yaml
edges:
  - {source: setup, target: train}            # ordering only, no artifacts
  - {source: prep, source_output: log, target: train, target_input: data,
     transfer: false}                         # ordering only, ids still checked
```

**Conditional edges.** A `condition` reads a JSON document from an upstream
output and decides whether the edge is live. A task runs if any incoming task
edge is live; otherwise it is skipped (`inactive`) and its downstream tasks
are treated the same way.

```yaml
  - source: evaluate
    target: deploy
    condition:
      kind: declarative        # required discriminator (python = Python-only)
      source_task: evaluate    # optional; defaults to the edge's source
      source_output: metrics   # required on an ordering-only edge
      key: scores.accuracy     # optional dotted path into the JSON
      op: gt                   # eq ne lt le gt ge in not_in contains truthy exists
      value: 0.9               # list for in/not_in; unused for truthy/exists
```

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

## Shared targets (`targets:` and `{ref}`)

`targets:` names target instances; a task whose `target` is `{ref: <name>}`
runs on that one instance, so tasks with the same ref share one machine.

```yaml
targets:
  box: {kind: local, working_directory: /scratch/run1}
tasks:
  - id: prep
    name: Prep
    kind: horus_task
    target: {ref: box}
    runtime: {kind: command, command: "echo prep"}
    executor: {kind: shell}
```

- Names match `^[a-z][a-z0-9_-]{0,62}$` (`InvalidTargetNameError`).
- `{ref}` takes no other keys; `{ref: box, kind: local}` is rejected. A
  `targets:` entry cannot itself be a ref (`TargetIsReferenceError`).
- Lookup is lexical: a subworkflow body's own `targets:` first, then the
  enclosing workflows' (inner names shadow outer). Unknown name:
  `UnknownTargetRefError`.
- A ref inside a `loop:` body raises `TargetRefInLoopError`; use an inline
  target there.
- The target can reject its own config or a member task
  (`SharedTargetError`), e.g. a GPU task on a machine without GPUs.
- Only releasable targets (e.g. the terraform plugin) are created once and
  released when no member is pending; see `execution.md`. For local/ssh/slurm
  a ref just saves repeating the target.

## Resources

`resources` is optional. Resource-aware targets read it, and it gates
placement once the workflow declares `capacity` for the task's location
(`location_id`, e.g. `local://<hostname>`): concurrent tasks on one location
never request more than its cap. A request above the location's total raises
`InsufficientCapacityError`. Without `capacity`, `local` ignores it. Unknown
keys are rejected (`extra="forbid"`).

```yaml
resources:
  cpus: 4          # int >= 1, or omit to let the target choose
  gpus: 1          # int >= 0, default 0
  memory_gb: 16    # int >= 1
  vram_gb: 24      # int >= 1
  walltime: "01:30:00"   # target-interpreted string
```

## Subworkflow (`sub:`)

A task with a `sub:` block inlines a complete child workflow when it runs. Its
ports come from the body: in-ports are the body's root `artifacts`, out-ports
are task outputs no inner edge consumes (named by artifact id). Parent edges
wire to those port ids; they are forced to ordering-only and re-emitted onto
the real inner tasks at run time. Inner task ids become `<id>/<inner id>`, and
inner relative paths land under `<run dir>/<id>/`.

```yaml
artifacts: [{id: text, kind: file, path: input.txt}]
tasks:
  - id: clean
    sub:
      kind: horus_workflow
      name: cleaner
      artifacts: [{id: raw, kind: file, path: raw.txt}]       # in-port "raw"
      tasks:
        - id: strip
          name: Strip
          kind: horus_task
          runtime: {kind: command, command: "tr -d ' ' < $raw_in > $cleaned"}
          executor: {kind: shell}
          inputs: [{id: raw_in, kind: file, path: raw_in.txt}]
          outputs: [{id: cleaned, kind: file, path: cleaned.txt}]  # out-port
      edges:
        - {source: artifact-raw, source_output: raw, target: strip, target_input: raw_in}
    port_overrides: {cleaned: clean_text}   # optional renames
    max_depth: 10                           # optional nesting guard (default 10)
  - id: final
    name: Final
    kind: horus_task
    runtime: {kind: command, command: "cat $txt > $final"}
    executor: {kind: shell}
    inputs: [{id: txt, kind: file, path: txt.txt}]
    outputs: [{id: final, kind: file, path: final.txt}]
edges:
  - {source: artifact-text, source_output: text, target: clean, target_input: raw}
  - {source: clean, source_output: clean_text, target: final, target_input: txt}
```

## Loop (`loop:`)

Repeats a body task while it says to continue, one iteration at a time. The
body writes `{"continue": true|false}` to the `until` output each iteration;
`index_input` (optional) receives the 0-based iteration index. Iteration ids
are `<id>#1`, `<id>#2`, ...; controller checks run as `<id>~1`, `<id>~2`, ...

```yaml
- id: refine
  loop:
    body:
      kind: horus_task
      runtime:
        kind: command
        command: >-
          i=$(cat $idx); if [ $i -ge 2 ]; then c=false; else c=true; fi;
          printf '{"continue": %s}' $c > $signal
      executor: {kind: shell}
      inputs: [{id: idx, kind: file, path: idx_in}]
      outputs: [{id: signal, kind: file, path: signal_out}]
    until: signal
    max_iterations: 10      # required hard cap, > 0
    index_input: idx
```

## Command substitution

`command` (and the script/python string runtimes) render `string.Template`
`$`/`${}` placeholders against the task before running. `str.format` `{}` passes
through untouched. Forms:

- `$id` / `${id}` — the artifact with that id, rendered as its path **on the
  task's target** (so a command written once runs unchanged local or remote),
  or its `value` for a value artifact.
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
tasks are skipped because their outputs exist and their manifests match; edit
an input or a command, delete the outputs, or pass `--no-skip-all` to force
re-execution.
