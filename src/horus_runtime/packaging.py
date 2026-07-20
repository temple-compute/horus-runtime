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
Bundle a workflow and the files it needs into a single zip.

A workflow directory is already self-contained: paths in the YAML resolve
against the directory holding it (see
:meth:`horus_runtime.core.workflow.base.BaseWorkflow._anchor_artifact`). This
module turns that directory into a zip carrying *only* the files the workflow
actually references, so it can be moved to a machine that never had the repo --
for example uploaded to a web UI that then runs it.

What travels: the workflow YAML, every external input artifact (one not
produced by any task), every ``python_script`` script, and every executor
``environment_file``. Run output is referenced only as task *outputs*, so it is
excluded by construction rather than by a filename blocklist.
"""

import zipfile
from pathlib import Path

from horus_builtin.runtime.substitution import is_template
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.i18n import tr as _

# Junk that can sit inside a referenced folder artifact but is never input.
_EXCLUDED_DIRS = frozenset(
    {".venv", "__pycache__", ".git", ".mypy_cache", ".ruff_cache"}
)


class BundleError(Exception):
    """A workflow could not be packaged."""


def collect_bundle_paths(
    workflow: "BaseWorkflow",
) -> tuple[list[Path], list[Path]]:
    """
    Return ``(required, artifacts)`` workflow-relative paths, sorted+deduped.

    *required* are files the author named directly -- scripts and executor
    environment files. They are never generated, so one that is absent is a
    broken workflow.

    *artifacts* are external input artifacts (not produced by any task). These
    are only *probably* input files: a plugin may re-pin a path into the run
    root when it expands (``map:`` does exactly this for its ``.gathered``
    folder and ``.over.marker``), which is not knowable before a run. A
    missing one is therefore reported as a warning, not an error.

    Absolute paths are skipped throughout: they name a location on this
    machine, cannot travel, and are the author's responsibility.
    """
    # Reuses the runtime's own produced-vs-external rule rather than
    # restating it here, so packaging can never drift from anchoring.
    produced = workflow._produced_declared_paths()  # noqa: SLF001
    artifact_paths: set[Path] = set()
    required: set[Path] = set()

    artifacts = [
        *workflow.artifacts,
        *(a for t in workflow.tasks for a in (*t.inputs, *t.outputs)),
    ]
    for artifact in artifacts:
        declared = artifact.declared_path
        # A produced path is run output; it is regenerated, never shipped.
        if declared is None or declared.is_absolute() or declared in produced:
            continue
        artifact_paths.add(declared)

    for task in workflow.tasks:
        for value in (
            getattr(task.runtime, "script", None),
            getattr(task.executor, "environment_file", None),
        ):
            if value is None or is_template(value):
                continue
            path = Path(value)
            if not path.is_absolute():
                required.add(path)

    # A file named as both a script and an artifact only travels once.
    artifact_paths -= required
    return sorted(required), sorted(artifact_paths)


def _expand(base: Path, rel: Path) -> list[Path]:
    """Expand a folder reference into its files; a file yields itself."""
    src = base / rel
    if not src.is_dir():
        return [rel]
    return [
        rel / child.relative_to(src)
        for child in sorted(src.rglob("*"))
        if child.is_file()
        and not _EXCLUDED_DIRS.intersection(child.relative_to(src).parts)
    ]


def package_workflow(
    workflow_yaml: Path, output: Path | None = None
) -> tuple[Path, list[Path], list[Path]]:
    """
    Write a zip bundling *workflow_yaml* and its referenced files.

    Returns ``(archive, members, skipped)`` -- the zip path, the
    workflow-relative paths written (excluding the YAML itself), and the
    artifact paths that did not exist and so were left out.

    Raises :class:`BundleError` if a script or environment file is missing, so
    a broken workflow fails here rather than halfway through a run.
    """
    workflow_yaml = workflow_yaml.resolve()
    base = workflow_yaml.parent
    workflow = BaseWorkflow.from_yaml(workflow_yaml)

    required, artifacts = collect_bundle_paths(workflow)

    missing = [rel for rel in required if not (base / rel).exists()]
    if missing:
        raise BundleError(
            _("Workflow references %(count)d missing file(s):\n%(files)s")
            % {
                "count": len(missing),
                "files": "\n".join(f"  {rel}" for rel in missing),
            }
        )

    present = [rel for rel in artifacts if (base / rel).exists()]
    skipped = [rel for rel in artifacts if not (base / rel).exists()]

    members = sorted(
        {m for rel in (*required, *present) for m in _expand(base, rel)}
    )
    output = output or base / f"{base.name}.zip"
    output = output.resolve()

    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED
    ) as bundle:
        # Always at the archive root under a fixed name, so an importer can
        # find the definition without guessing the original filename.
        bundle.write(workflow_yaml, "workflow.yaml")
        for rel in members:
            bundle.write(base / rel, rel.as_posix())

    return output, members, skipped
