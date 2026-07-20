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
Tests for bundling a workflow and its input files into a zip.
"""

import zipfile
from pathlib import Path

import pytest

from horus_runtime.packaging import BundleError, package_workflow

# A producer -> consumer pair where the consumer also reads a committed input
# file. ``results/data.txt`` is a task output, so it must never be bundled.
WORKFLOW = """
kind: horus_workflow
name: Bundle Me (v1)
tasks:
  - kind: horus_task
    id: produce
    name: Produce
    inputs:
      - id: seed
        kind: file
        path: examples/seed.txt
    outputs:
      - id: data
        kind: file
        path: results/data.txt
    executor:
      kind: shell
    runtime:
      kind: python_script
      script: scripts/produce.py
      args: --seed ${seed} --out ${data}
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
    outputs:
      - id: report
        kind: file
        path: results/report.txt
    executor:
      kind: shell
    runtime:
      kind: command
      command: cat ${data} ${config} > ${report}
    target:
      kind: local
edges:
  - source: produce
    source_output: data
    target: consume
    target_input: data
"""


@pytest.fixture
def workflow_dir(tmp_path: Path) -> Path:
    """A workflow directory laid out the way Pantheon workflows are."""
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "seed.txt").write_text("seed\n")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "run.yaml").write_text("k: v\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "produce.py").write_text("pass\n")

    # Output from a previous run: present on disk, must not be bundled.
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "data.txt").write_text("stale\n")

    (tmp_path / "workflow.yaml").write_text(WORKFLOW)
    return tmp_path


def _members(archive: Path) -> set[str]:
    with zipfile.ZipFile(archive) as bundle:
        return set(bundle.namelist())


@pytest.mark.usefixtures("horus_context")
def test_bundles_inputs_and_scripts_but_not_outputs(
    workflow_dir: Path,
) -> None:
    """Committed inputs and scripts travel; run output does not."""
    archive, members, skipped = package_workflow(
        workflow_dir / "workflow.yaml"
    )

    assert _members(archive) == {
        "workflow.yaml",
        "examples/seed.txt",
        "configs/run.yaml",
        "scripts/produce.py",
    }
    # results/data.txt exists on disk but is produced by `produce`, so it is
    # regenerated rather than shipped -- and it is not a missing input either.
    assert Path("results/data.txt") not in members
    assert skipped == []


@pytest.mark.usefixtures("horus_context")
def test_missing_script_is_an_error(workflow_dir: Path) -> None:
    """A script is authored, never generated, so its absence is fatal."""
    (workflow_dir / "scripts" / "produce.py").unlink()

    with pytest.raises(BundleError, match=r"scripts/produce\.py"):
        package_workflow(workflow_dir / "workflow.yaml")


@pytest.mark.usefixtures("horus_context")
def test_missing_input_artifact_is_skipped_not_fatal(
    workflow_dir: Path,
) -> None:
    """
    A plugin may pin an input path into the run root when it expands (``map:``
    does this), so an absent input artifact is reported, not fatal.
    """
    (workflow_dir / "configs" / "run.yaml").unlink()

    archive, _members_out, skipped = package_workflow(
        workflow_dir / "workflow.yaml"
    )

    assert skipped == [Path("configs/run.yaml")]
    assert "configs/run.yaml" not in _members(archive)
    assert "examples/seed.txt" in _members(archive)


FOLDER_WORKFLOW = """
kind: horus_workflow
name: Folder Input
tasks:
  - kind: horus_task
    id: consume
    name: Consume
    inputs:
      - id: dataset
        kind: folder
        path: examples
    outputs:
      - id: report
        kind: file
        path: results/report.txt
    executor:
      kind: shell
    runtime:
      kind: command
      command: ls ${dataset} > ${report}
    target:
      kind: local
"""


@pytest.mark.usefixtures("horus_context")
def test_folder_input_is_expanded_and_junk_excluded(tmp_path: Path) -> None:
    """A folder input ships its files, minus caches that are never input."""
    (tmp_path / "examples" / "nested").mkdir(parents=True)
    (tmp_path / "examples" / "seed.txt").write_text("seed\n")
    (tmp_path / "examples" / "nested" / "more.txt").write_text("x\n")
    (tmp_path / "examples" / "__pycache__").mkdir()
    (tmp_path / "examples" / "__pycache__" / "c.pyc").write_text("x\n")
    (tmp_path / "workflow.yaml").write_text(FOLDER_WORKFLOW)

    archive, _members_out, _skipped = package_workflow(
        tmp_path / "workflow.yaml"
    )
    names = _members(archive)

    assert "examples/seed.txt" in names
    assert "examples/nested/more.txt" in names
    assert not any("__pycache__" in n for n in names)


@pytest.mark.usefixtures("horus_context")
def test_output_defaults_beside_the_workflow(workflow_dir: Path) -> None:
    """With no -o, the zip is named after the workflow directory."""
    archive, _members_out, _skipped = package_workflow(
        workflow_dir / "workflow.yaml"
    )
    assert archive == (workflow_dir / f"{workflow_dir.name}.zip").resolve()
