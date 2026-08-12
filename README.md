# Horus Runtime

**Reproducible scientific workflows, from your laptop to the HPC cluster.**

[![PyPI](https://img.shields.io/pypi/v/horus-runtime?color=blue)](https://pypi.org/project/horus-runtime/)
[![Python](https://img.shields.io/pypi/pyversions/horus-runtime)](https://pypi.org/project/horus-runtime/)
[![CI](https://img.shields.io/github/actions/workflow/status/temple-compute/horus-runtime/python-ci.yaml?branch=main&label=CI)](https://github.com/temple-compute/horus-runtime/actions/workflows/python-ci.yaml)
[![Downloads](https://img.shields.io/pypi/dm/horus-runtime?color=blue)](https://pypi.org/project/horus-runtime/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Docs](https://img.shields.io/badge/docs-templecompute.com-blue)](https://docs.templecompute.com)
[![Discussions](https://img.shields.io/github/discussions/temple-compute/horus-runtime)](https://github.com/temple-compute/horus-runtime/discussions)

Horus Runtime is the open-source engine behind the [Temple Compute](https://www.templecompute.com)
platform: you describe a pipeline once, in YAML or Python, and run it wherever the compute
lives. It tracks every artifact a task produces, moves those artifacts between machines
for you, and skips work that is already done when you run it again.

<img width="1600" height="1202" alt="Horus Runtime TUI" src="https://github.com/user-attachments/assets/8f6a5f77-c0fa-48bf-b82b-5683af1caec4" />

## Quickstart

Install it:

```bash
uv add horus-runtime     # or: pip install horus-runtime
```

Save this as `workflow.yaml`:

```yaml
name: example_pipeline
kind: horus_workflow

tasks:
  - id: producer
    name: Produce data
    kind: horus_task
    runtime:
      kind: command
      # $data renders to the on-target path of the "data" output artifact.
      command: "mkdir -p /tmp/horus_example && echo 42 > $data"
    executor:
      kind: shell
    target:
      kind: local
    outputs:
      - id: data
        kind: file
        path: /tmp/horus_example/data.txt

  - id: consumer
    name: Summarize data
    kind: horus_task
    runtime:
      kind: command
      command: "wc -l ${data_in} > $summary"
    executor:
      kind: shell
    target:
      kind: local
    inputs:
      - id: data_in
        kind: file
        path: /tmp/horus_example/data.txt
    outputs:
      - id: summary
        kind: file
        path: /tmp/horus_example/summary.txt

# Edges are the DAG: producer.data feeds consumer.data_in, so producer runs
# first and its output is transferred before the consumer starts.
edges:
  - source: producer
    source_output: data
    target: consumer
    target_input: data_in
```

Run it:

```bash
horus run workflow.yaml
```

```
[HorusTask._run] Task Produce data started.
[TaskTimeMiddleware.after] Task Produce data completed in 0.01 seconds.
[HorusTask._run] Task Summarize data started.
[TaskTimeMiddleware.after] Task Summarize data completed in 0.03 seconds.
[WorkflowTimeMiddleware.after] Workflow example_pipeline completed in 0.05 seconds.
```

Run it a second time and nothing re-executes, because both tasks' output artifacts already
exist:

```
[BaseTask.run] Skipping task Produce data. Already complete.
[BaseTask.run] Skipping task Summarize data. Already complete.
```

Delete the outputs, or pass `--no-skip-all`, to force a fresh run.

## Why Horus

- **Write once, run anywhere.** A task declares *what* to run (`runtime`), *how* to run it
  (`executor`), and *where* (`target`). Moving a stage from your laptop to a SLURM cluster
  is a change to the `target` block, not a rewrite.
- **Artifacts, not filenames.** Inputs and outputs are typed, addressable objects. Horus
  resolves them into your commands, transfers them between targets when an edge crosses
  machines, and knows when they already exist.
- **Restartable by default.** Completed tasks are skipped on re-run, so a pipeline that
  failed at hour nine picks up at hour nine.
- **Everything is a plugin.** Targets, runtimes, executors, artifacts, tasks, transfer
  strategies, and middleware are all registered `kind`s. Adding one is a package with an
  entry point, never a fork.
- **Built for real science.** Resource requests, subworkflows, mapped fan-out, run
  packaging, and a live TUI, driven from a file you can commit and diff.

## CLI

| Command | What it does |
| --- | --- |
| `horus run WORKFLOW_YAML` | Execute the workflow. `--trigger` picks the starting task, `--no-tui` streams plain logs, `--no-skip TASK_ID` / `--no-skip-all` force re-runs, `--debug` turns on debug logging. |
| `horus package WORKFLOW_YAML` | Bundle the workflow and every file it references into a zip, so it runs on a machine that never had your directory. |
| `horus sanitize WORKFLOW_YAML` | Promote implicit root inputs into declared top-level artifacts, which is what lets a UI offer them as workflow inputs. |

Full CLI and SDK reference: [docs.templecompute.com](https://docs.templecompute.com).

## Built-in kinds

Everything below ships with the runtime and is usable straight after install.

| Category | Kinds |
| --- | --- |
| Targets | `local` |
| Runtimes | `command`, `python`, `python_string`, `python_script` |
| Executors | `shell`, `python_exec`, `python_fn`, `python_fn_external` |
| Artifacts | `file`, `folder`, `json`, `pickle`, `number`, `boolean`, `string` |
| Tasks | `horus_task`, `subworkflow` |
| Workflows | `horus_workflow` |

## Ecosystem

Official plugins, installable alongside the runtime:

| Plugin | Adds |
| --- | --- |
| [horus-slurm](https://github.com/temple-compute/horus-slurm) | Run tasks as SLURM jobs on an HPC cluster. |
| [horus-docker](https://github.com/temple-compute/horus-docker) | Execute tasks inside Docker containers. |
| [horus-singularity](https://github.com/temple-compute/horus-singularity) | Execute tasks inside Singularity/Apptainer containers. |
| [horus-environments](https://github.com/temple-compute/horus-environments) | Auto-provision per-task Python environments with `uv` or conda. |

And for ready-made science: [**Pantheon**](https://github.com/temple-compute/pantheon) is a
curated library of production workflows, from drug discovery (Boltz-2 virtual screening,
AutoDock Vina docking) to molecular dynamics setup with BioExcel Building Blocks (GROMACS,
AMBER). Contributions welcome there too.

## Writing your own plugin

A plugin is an ordinary Python package that declares a `horus.*` entry point:

```toml
[project.entry-points."horus.target"]
my_cluster = "my_package.target"
```

Once installed, `kind: my_cluster` works in any workflow. Start from the
[plugin template](https://github.com/temple-compute/horus-runtime-plugin) and see the
[SDK docs](https://docs.templecompute.com) for the base classes.

## Contributing

Contributions are very welcome, from bug reports to new plugins.

- Read [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and the PR checklist.
- Browse [good first issues](https://github.com/temple-compute/horus-runtime/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
- Ask questions in [Discussions](https://github.com/temple-compute/horus-runtime/discussions).
- Be excellent to each other: [Code of Conduct](CODE_OF_CONDUCT.md).

Found a security issue? Please follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Funding & Credits

Developed by [Temple Compute](https://www.templecompute.com).

## License

`horus-runtime` is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
See the [LICENSE](LICENSE) file for details.

For commercial licensing and support, contact Temple Compute at
[christian@templecompute.com](mailto:christian@templecompute.com).
