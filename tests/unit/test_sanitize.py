#
# horus-runtime
# Copyright (C) 2026 Temple Compute
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""
Tests for promoting implicit root inputs into declared root artifacts.
"""

from pathlib import Path

import pytest
import yaml

from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.sanitize import find_root_inputs, sanitize_workflow

# `seed` and `config` are unwired inputs (root inputs). `data` is wired by an
# edge. `shared.yaml` is read by both tasks, so it is one artifact, two edges.
WORKFLOW = """
kind: horus_workflow
name: Sanitize Me
# A comment the rewrite must not eat.
_executor: &executor
  kind: shell
tasks:
  - kind: horus_task
    id: produce
    name: Produce
    inputs:
      - id: seed
        kind: file
        path: examples/seed.txt
      - id: shared
        kind: file
        path: configs/shared.yaml
    outputs:
      - id: data
        kind: file
        path: results/data.txt
    executor: *executor
    runtime:
      kind: command
      command: cat ${seed} ${shared} > ${data}
    target:
      kind: local
  - kind: horus_task
    id: consume
    name: Consume
    inputs:
      - id: data
        kind: file
        path: results/data.txt
      - id: config
        kind: file
        path: configs/run.yaml
      - id: shared
        kind: file
        path: configs/shared.yaml
    outputs:
      - id: report
        kind: file
        path: results/report.txt
    executor: *executor
    runtime:
      kind: command
      command: cat ${data} ${config} ${shared} > ${report}
    target:
      kind: local
edges:
  - source: produce
    source_output: data
    target: consume
    target_input: data

orchestrator_target:
  kind: local
"""


@pytest.fixture
def workflow_dir(tmp_path: Path) -> Path:
    """A workflow directory laid out the way Pantheon workflows are."""
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "seed.txt").write_text("seed\n")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "run.yaml").write_text("k: v\n")
    (tmp_path / "configs" / "shared.yaml").write_text("k: v\n")
    (tmp_path / "workflow.yaml").write_text(WORKFLOW)
    return tmp_path


@pytest.mark.usefixtures("horus_context")
def test_finds_unwired_inputs_only(workflow_dir: Path) -> None:
    """An edge-fed input is not a root input; an unwired one is."""
    workflow = BaseWorkflow.from_yaml(workflow_dir / "workflow.yaml")
    roots, missing = find_root_inputs(workflow)

    assert {r.path.as_posix() for r in roots} == {
        "examples/seed.txt",
        "configs/run.yaml",
        "configs/shared.yaml",
    }
    assert missing == []


@pytest.mark.usefixtures("horus_context")
def test_shared_path_is_one_artifact_many_edges(workflow_dir: Path) -> None:
    """One file read by two tasks travels once and wires twice."""
    workflow = BaseWorkflow.from_yaml(workflow_dir / "workflow.yaml")
    roots, _ = find_root_inputs(workflow)

    shared = next(r for r in roots if r.path.name == "shared.yaml")
    assert shared.consumers == (("produce", "shared"), ("consume", "shared"))


@pytest.mark.usefixtures("horus_context")
def test_unwired_produced_path_is_a_missing_edge(workflow_dir: Path) -> None:
    """An unwired input a task produces needs an edge, not promotion."""
    text = (workflow_dir / "workflow.yaml").read_text()
    # Drop the only edge, orphaning `consume.data` -- a path `produce` makes.
    text = text.split("edges:")[0] + "edges: []\n"
    (workflow_dir / "workflow.yaml").write_text(text)

    workflow = BaseWorkflow.from_yaml(workflow_dir / "workflow.yaml")
    roots, missing = find_root_inputs(workflow)

    assert [(m.task_id, m.input_id, m.producer) for m in missing] == [
        ("consume", "data", "produce")
    ]
    assert all(r.path.as_posix() != "results/data.txt" for r in roots)


@pytest.mark.usefixtures("horus_context")
def test_sanitized_workflow_declares_roots_and_still_validates(
    workflow_dir: Path,
) -> None:
    """The rewrite round-trips: roots declared, edges wired, model valid."""
    written, promoted, _ = sanitize_workflow(workflow_dir / "workflow.yaml")

    assert written.name == "workflow.sanitized.yaml"
    doc = yaml.safe_load(written.read_text())
    assert {a["id"] for a in doc["artifacts"]} == {"seed", "config", "shared"}
    # Paths stay relative: an absolute one could not travel in a bundle.
    assert all(not a["path"].startswith("/") for a in doc["artifacts"])

    # 1 original edge + 1 seed + 1 config + 2 shared consumers.
    assert len(doc["edges"]) == 5
    root_edges = {
        (e["source"], e["source_output"], e["target"], e["target_input"])
        for e in doc["edges"]
        if e["source"].startswith("artifact-")
    }
    assert ("artifact-seed", "seed", "produce", "seed") in root_edges

    # Re-validates, and the promoted set matches what was written.
    reloaded = BaseWorkflow.from_yaml(written)
    assert {a.id for a in reloaded.artifacts} == {"seed", "config", "shared"}
    assert len(promoted) == 3


@pytest.mark.usefixtures("horus_context")
def test_rewrite_is_additive(workflow_dir: Path) -> None:
    """Comments and the executor anchor survive: nothing is re-dumped."""
    written, _, _ = sanitize_workflow(workflow_dir / "workflow.yaml")
    text = written.read_text()

    assert "# A comment the rewrite must not eat." in text
    assert "&executor" in text
    assert "*executor" in text


@pytest.mark.usefixtures("horus_context")
def test_accept_limits_promotion(workflow_dir: Path) -> None:
    """Declining a candidate leaves it implicit."""
    written, promoted, _ = sanitize_workflow(
        workflow_dir / "workflow.yaml", accept={"config"}
    )

    assert [r.root_id for r in promoted] == ["config"]
    doc = yaml.safe_load(written.read_text())
    assert {a["id"] for a in doc["artifacts"]} == {"config"}


@pytest.mark.usefixtures("horus_context")
def test_nothing_to_promote_writes_nothing(workflow_dir: Path) -> None:
    """A workflow with every input wired is left alone."""
    written, promoted, _ = sanitize_workflow(
        workflow_dir / "workflow.yaml", accept=set()
    )

    assert promoted == []
    assert written == (workflow_dir / "workflow.yaml").resolve()
    assert not (workflow_dir / "workflow.sanitized.yaml").exists()
