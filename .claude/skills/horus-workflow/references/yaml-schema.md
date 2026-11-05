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
max_concurrency: 4         # optional; plain tasks dispatched at once. unset = 8, 0 = no limit
failure_policy: fail_fast  # optional; fail_fast (default) | continue
cache: deep                # optional; deep (default) | fast. How tasks check their inputs
capacity:                  # optional; per-location resource caps (see Resources)
  "local://myhost": {cpus: 16, gpus: 2}
```

- `failure_policy: fail_fast` cancels every in-flight task on the first
  failure. `continue` lets unrelated branches finish; the failed task's
  descendants end `SKIPPED` (`upstream_failed`) and the workflow still ends
  `FAILED`.
- `max_concurrency` does not count maps or their clones. Maps start
  without waiting for a slot, and each caps only its own clones, so six maps
  can run 48 clones at once. On a laptop or shared host, set `capacity` to
  bound the total.
- A chained map keeps each source map's machine until its own last item has
  read from it, so a slow consumer keeps a fast source's machine running.
- `cache` is the default input check for every task; a task's own `cache:`
  wins. See [Cache modes](#cache-modes-deep-and-fast).
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
  cache: fast             # optional; deep | fast. Unset = the workflow's cache
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
  chain: false            # optional; start each item once its source map has it
  zip: []                 # optional; more inputs paired by item, [{input_id, as}]
  drop_exit_codes: []     # optional; exit codes that drop the item, 1 to 255
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
and completes the map, unless every item was dropped. The clone stays `FAILED`.
Only use it when items are independent. Under `failure_policy: continue` or
`drop`, one failing clone does not cancel the others.

The map only fans out. Producing the collection is whatever upstream task
writes the iterable artifact; folding the slots back into one value is
whatever downstream task consumes the folder output.

### Chained maps (`chain`, `zip`, `drop_exit_codes`)

Items match by slot name. A map over another map's output keeps that map's
slot names, so every map fed from one head map shares one item space: slot
`03` is the same item all the way down. The head names its slots by index
over its sorted items. If the head list changes (an item added or removed,
or the count crosses 10, 100, ...), the later items get new slot names and
run again down the chain.

**`chain: true`.** By default a map starts when its source map has finished.
A chained map starts clone `B[X]` as soon as `A[X]` is done, while A's other
items still run. Example, the cMD workflow (pantheon
`workflows/drug-discovery/w04-cmd`): each replica runs equilibrate, then
produce, then analyze, and no replica waits for the slowest equilibration.

```yaml
tasks:
  - kind: horus_map
    id: equilibrate
    over: {input_id: replicas_in, as: slot}
    # ... writes ${equilibrated}, one slot per replica
  - kind: horus_map
    id: produce
    name: Run production MD per replica
    chain: true
    over: {input_id: equilibrated_in, as: slot}
    inputs: [{id: equilibrated_in, kind: folder, path: equilibrated_out/}]
    outputs: [{id: produced, kind: folder, path: produced_out/}]
    runtime:
      kind: command
      command: "bash ${slot}/production.sh ${slot} ${slot}/preprod ${produced}"
    executor: {kind: shell}
  - kind: horus_map
    id: analyze
    chain: true
    over: {input_id: produced_in, as: slot}
    # ... reads ${slot}, writes ${analyzed}
edges:
  - {source: equilibrate, source_output: equilibrated, target: produce, target_input: equilibrated_in}
  - {source: produce, source_output: produced, target: analyze, target_input: produced_in}
```

Rules for a chained map (checked at load):

- `over` and every `zip` input must come from a map through a transfer edge.
- The body reads a streamed input only as `$<as>`: the whole folder is not
  there yet. A command body that uses `$<input id>`, `${task.inputs}` or
  `${task.inputs.<streamed id>}` is rejected. Outputs, other inputs and other
  `${task...}` names are fine.
- A Python body is not checked. It must read only its `as` parameter, never
  the streamed folder.
- An edge from a chained source cannot have a `condition`.
- A chained source feeds only the `over` or `zip` input; no other input of
  the map can take all of its output.

If a source map loses its machine and runs an item again, a chained clone
that already took that item reads the evacuated copy or fails with the lost
output; `on_item_failure` applies.

**`zip`.** Pairs more inputs by item: slot X of each zip input is bound as
`$<as>` in clone X. Each zip input must come from a map in the same item
space. It works with or without `chain`.

```yaml
- kind: horus_map
  id: report_qc        # bams from map align, stats from map qc, both over samples
  name: Report QC per sample
  over: {input_id: bams, as: bam}
  zip:
    - {input_id: stats, as: stat}
  inputs:
    - {id: bams, kind: folder, path: bams/}
    - {id: stats, kind: folder, path: stats/}
  outputs: [{id: reports, kind: folder, path: reports/}]
  runtime: {kind: command, command: "qc_report $bam $stat > $reports/report.txt"}
  executor: {kind: shell}
```

A zip slot the `over` input lacks was dropped on that side, so it is a gap
and is ignored. An `over` slot a zip input lacks was dropped on the zip side.

**`drop_exit_codes`.** A clone that exits with one of these codes drops its
item instead of failing, whatever `on_item_failure` says. It never retries.

```yaml
- kind: horus_map
  id: filter
  drop_exit_codes: [10]
  runtime: {kind: command, command: "check $sample || exit 10; ..."}
```

Drops:

- A clone dropped by exit code ends `SKIPPED` with `skip_reason: dropped`
  and the error `Dropped: exit code N`.
- Clones downstream of a dropped item never run. They end `SKIPPED` with
  `upstream_dropped`, and their slot stays a gap.
- A map completes with its drops removed from its output folder. The run
  still ends `COMPLETED`. Dropped items run again on the next run.
- If every item was dropped, the map fails with `All N items were dropped
  in <map>.` and its consumers do not run.

Exit code caveats:

- Only command bodies match. A Python body that calls `sys.exit(10)` fails
  the item.
- Codes depend on the backend. Slurm reports 1 for a timeout, OOM, node
  failure or cancel. Docker uses 125 to 127 for its own errors. Avoid those
  codes, and pick one your command does not return for other reasons, such
  as 10.

An older runtime ignores `chain`, `zip` and `drop_exit_codes`: it runs the
map as a barrier, leaves a zip's `$<as>` unrendered, and treats a drop code
as a plain failure.

## Artifact

Artifacts appear in a task's `inputs`/`outputs` or as workflow-level root
`artifacts`. Fields:

```yaml
- id: result             # required; stable id (edge endpoint key)
  kind: file             # file | folder | json | pickle | number | boolean | string | choice | multichoice | list | <plugin kind>
  path: out/result.txt   # required; absolute, or relative (see Paths)
  name: ""               # optional; defaults to id
  description: ""         # optional
```

Output existence plus the task manifest (input digests + config hash) decides
completion.

### Cache modes (`deep` and `fast`)

`cache` sets how input digests are taken. It is read from the task, then the
workflow, then defaults to `deep`. Map clones use the map's `cache`.

- `deep` hashes every byte of every input. Safe, but a large input is read
  again on each run.
- `fast` hashes each file's path, size and mtime in whole seconds, plus the
  content of files under 1 MiB. Large files are not read.

`fast` caveats:

- A rewrite with the same size in the same second is not seen (false hit).
- A `touch`, or a copy that drops mtime, re-runs the task (false miss). Copy
  inputs with their mtime (`cp -p`, `rsync -t`). Local and tar copies made
  by the runtime keep it.
- On a network filesystem (NFS), attribute caching can show a stale mtime
  for a short time after another host writes the file.

Each input is digested once per run and mode. A task forgets the digests of
its outputs when it is dispatched, so a program that writes to a path it does
not declare as an output can leave a stale digest for the rest of the run.

`cache` is not part of the config hash, but the digest records its mode, so
switching a task's mode re-runs it once.

**Upgrading to 0.9.0:** folder digests changed format. Every task with a
folder input (chained maps too) re-runs once after the upgrade. Tasks with only
file inputs do not.

### Paths

Relative paths are allowed and anchored at run start: a path some task
produces goes under the run directory, any other path under the base
directory. The base directory is the YAML's folder (process CWD for a
Python-built workflow); the run directory is the base joined with the
orchestrator target's `working_directory`, which is the base itself by
default. Absolute paths are left as they are. Each task runs in
`<target working dir>/<task_id>/<execution id>/`.

### Value artifacts

`number`, `boolean`, `string`, `choice`, `multichoice` and `list` carry an
inline `value`. `$id` renders the **value**, not a path; the value is also written to `path` the
first time a task consumes it; read that file with `${id.path}`.

```yaml
artifacts:
  - {id: steps, kind: number, path: steps.json, value: 1000}
  - {id: temp, kind: number, path: temp.json, value: 300, min: 250, max: 400, step: 5}
  - {id: label, kind: string, path: label.txt, value: run-a}
  - {id: pdb, kind: string, path: pdb.txt, value: 1abc, pattern: '[0-9][a-z0-9]{3}'}
  - {id: notes, kind: string, path: notes.txt, value: "", max_length: 500, multiline: true}
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
  - id: elements              # any subset of the options
    kind: multichoice
    path: elements.json
    options: [{value: H}, {value: C}, {value: O, label: Oxygen}]
    value: [H, O]
  - {id: seeds, kind: list, path: seeds.json, value: [1, 2, 3]}
```

- `boolean`: map both `on` and `off` or neither; mapped values must differ
  and cannot be `true`/`false`. `$id` renders `true`/`false` when unmapped.
  Quote the `"on"`/`"off"` keys: unquoted, PyYAML reads them as `True`/`False`
  and the mapping is silently dropped.
- `choice`: `options` must be non-empty with distinct values; `value` must be
  one of them.
- `number`: `min`/`max` are inclusive; `step` counts from `min` (or 0), so
  `step: 1` with an integer `min` allows only integers. An unset `value`
  starts at `min`.
- `string`: `pattern` is a Python regex that must match the whole value;
  single-quote it in YAML so backslashes survive. `max_length` counts
  characters. `multiline` only changes the editor.
- `multichoice`: same `options` rules as `choice`; `value` is any subset
  (empty is fine), kept in option order.
- `multichoice`/`list`: `$id` renders the items as space-separated,
  shell-quoted words (`--seeds $seeds` gives `--seeds 1 2 3`); the file at
  `${id.path}` is a JSON array.
- Quote option values YAML would read as booleans (`"yes"`, `"on"`).
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

`cache` precedence for an inner task: its own `cache:`, then the body's
(`sub.cache`), then the subworkflow task's own `cache:` (next to `sub:`, or
on the long form `kind: subworkflow` with a `body:` block), then the
workflow's.

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

Set `cache:` inside `loop.body`; a `cache:` on the outer loop entry is dropped.

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
