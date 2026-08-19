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
Declarative map / fan-out / fan-in construct.

A ``map`` task expands, once its (optional) source collection is ready,
into N clones of a template task, dispatches them, then fans their outputs
into a pre-existing "gather" task. The collection is either an upstream
task's output, a workflow *root* artifact, or simply an integer range (see
:class:`MapOver`). It is expressed either as a ``map:``
block in YAML (lowered by :func:`lower_map_entry`, hooked into
:class:`~horus_runtime.core.workflow.base.BaseWorkflow`'s ``model_validate``
pipeline) or via the :func:`map_task` Python builder (``wf.map(...)``).

Both authoring paths produce the same object: a :class:`MapExpander`, a
registered ``kind="map_expander"`` task whose fan-out behaviour is entirely
declarative (:class:`MapOver` plus a plain ``dict`` template), so it
round-trips through ``to_yaml``/``from_yaml`` like any other task — unlike a
Python-closure task, whose function cannot serialize.

Fan-out/fan-in wiring
----------------------
:meth:`MapExpander._run` reads or counts the source collection, then builds
N clone tasks with deterministic, zero-padded ids
(``f"{id}[{i:0{width}d}]"``) and materializes each clone's slice/index
directly on the workflow's orchestrator filesystem (bypassing the generic
edge-transfer path, which would otherwise repoint every clone's input at
the *whole* collection rather than its own slice — see the module-level
note on ``transfer=False`` below). All of it is committed atomically via
:meth:`~horus_runtime.core.workflow.base.BaseWorkflow.expand`.

Every edge the expander creates is deliberately ``transfer=False``
(ordering-only):

- it still makes each clone a real dependent of the expander in the
  dependency graph, so the clones (and, transitively, the gather task) fall
  inside the scheduler's trigger-reachable scope, which only follows
  task-to-task edges;
- but it is excluded from
  :meth:`~horus_runtime.core.workflow.base.BaseWorkflow._build_source_map`,
  so the generic artifact-transfer step never repoints a clone's
  already-sliced input (or the gather task's fan-in folder) back at
  whichever artifact the edge nominally "sources" from.

Because the expander itself declares no meaningful output (only an
internal wiring marker that is never written), it never reports complete
and always re-runs, deterministically re-deriving the same clone set; each
clone is independently skipped via ordinary ``skip_if_complete`` behaviour
if its own output already exists, which is what makes a partially
completed map resumable.

Template inputs other than the per-item and per-index inputs are different:
they are *shared* inputs.  The expander exposes those inputs as ordinary
ports, so an author can wire a script, environment, parameter file, or any
other common dependency directly to the map on the workflow canvas.  The
incoming edge is ordering-only on the expander itself (the expander does not
consume the artifact); when it expands, it is re-emitted as a data-carrying
edge onto every clone.
"""

import json
import shlex
import shutil
import tarfile
import uuid
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel, Field, model_validator

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.store import ArtifactStore
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger

if TYPE_CHECKING:
    from horus_runtime.core.workflow.base import BaseWorkflow

_FANOUT_SUFFIX = ".fanout"
_ITEMS_DIR_SUFFIX = ".items"
_GATHERED_DIR_SUFFIX = ".gathered"


class MapConfigurationError(WorkflowError):
    """Raised when a ``map:`` block or ``wf.map(...)`` call is malformed."""


class MapOver(BaseModel):
    """
    Declarative fan-out specification for :class:`MapExpander`.

    Exactly one mode applies:

    - **Collection mode** (``source_task``, ``source_output`` and
      ``item_input`` all set): fan out over the items of an upstream task's
      output collection. A :class:`.FolderArtifact` collection fans out over
      its children, sorted by name; a JSON-list collection fans out over its
      elements, in list order.
    - **Artifact mode** (``source_artifact`` and ``item_input`` set): fan out
      over the items of a workflow *root* artifact, enumerated exactly as in
      collection mode. This is the canvas's "fan out over a folder or list
      already on the board" case, where no producer task exists.
    - **Range mode** (``range`` set, every source field unset): fan out over
      ``range(0, range)``, feeding each clone only its integer index via
      ``index_input``.

    ``index_input`` may additionally be set in collection or artifact mode to
    also give each clone its own numeric index alongside its sliced item.
    """

    source_task: str | None = None
    """Id of the upstream task producing the collection (collection mode)."""

    source_output: str | None = None
    """Output artifact id on ``source_task`` holding the collection."""

    source_artifact: str | None = None
    """Id of the workflow root artifact holding the collection (artifact
    mode). Mutually exclusive with ``source_task``/``source_output``."""

    item_input: str | None = None
    """Input id on the template that receives the i-th sliced item."""

    index_input: str | None = None
    """Input id on the template that receives the integer index ``i``."""

    range: int | None = None
    """Clone count (range mode); ``None`` in collection mode."""

    @model_validator(mode="after")
    def _check_mode(self) -> Self:
        """
        Enforce exactly one of collection / artifact / range mode.

        The three are mutually exclusive rather than merely pairwise
        distinct: naming both a producer task and a root artifact would
        leave the collection to enumerate genuinely ambiguous, and pairing
        either with ``range`` would make the clone count so.
        """
        picked = [
            mode
            for mode, chosen in (
                (
                    "collection",
                    self.source_task is not None
                    or self.source_output is not None,
                ),
                ("artifact", self.source_artifact is not None),
                ("range", self.range is not None),
            )
            if chosen
        ]
        if len(picked) != 1:
            raise ValueError(
                _(
                    "MapOver needs exactly one of collection mode "
                    "('source_task' + 'source_output' + 'item_input'), "
                    "artifact mode ('source_artifact' + 'item_input') or "
                    "range mode ('range' alone); got: %(picked)s."
                )
                % {"picked": ", ".join(picked) if picked else _("none")}
            )

        if picked[0] == "collection" and not all(
            f is not None
            for f in (self.source_task, self.source_output, self.item_input)
        ):
            raise ValueError(
                _(
                    "MapOver collection mode requires 'source_task', "
                    "'source_output' and 'item_input' together."
                )
            )
        if picked[0] == "artifact" and self.item_input is None:
            raise ValueError(_("MapOver artifact mode requires 'item_input'."))
        if picked[0] == "range" and self.item_input is not None:
            raise ValueError(
                _(
                    "MapOver range mode has no collection to slice, so it "
                    "cannot set 'item_input'; use 'index_input'."
                )
            )
        if self.item_input is not None and self.item_input == self.index_input:
            # _run pins the slice, then pins the index over the same artifact,
            # so a clone would silently receive its index where its slice
            # should be -- wrong results with nothing to notice.
            raise ValueError(
                _(
                    "MapOver cannot send both the item and the index to "
                    "'%(input)s': one would overwrite the other. Give the "
                    "index its own template input, or drop it."
                )
                % {"input": self.item_input}
            )
        return self

    @property
    def is_range(self) -> bool:
        """Whether this spec is in range mode."""
        return self.range is not None

    @property
    def is_artifact(self) -> bool:
        """Whether this spec enumerates a workflow root artifact."""
        return self.source_artifact is not None

    @property
    def is_collection(self) -> bool:
        """Whether this spec enumerates an upstream task's output."""
        return self.source_task is not None


class MapExpander(HorusTask):
    """
    Fans a template task out into N clones, then fans their outputs into a
    gather task. See the module docstring for the full wiring story.
    """

    kind: str = "map_expander"
    kind_name: ClassVar[str] = "Map Expander"
    kind_description: ClassVar[str] = _(
        "Fans a template task out into N clones over a source collection "
        "or an integer range, then fans their outputs into a gather task."
    )

    runtime: BaseRuntime = Field(
        default_factory=lambda: CommandRuntime(command="true")
    )
    """
    Inert placeholder: :meth:`_run` is fully overridden and never delegates
    to ``self.executor``/``self.runtime``, so these exist only to satisfy
    ``BaseTask``'s required fields.
    """

    executor: BaseExecutor = Field(default_factory=ShellExecutor)
    target: BaseTarget = Field(default_factory=LocalTarget)

    over: MapOver
    """The fan-out specification (collection or range mode)."""

    template: dict[str, Any]
    """
    Declarative body of the per-clone task: everything a task needs minus
    ``id``/``name`` (``kind``, ``inputs``, ``outputs``, ``runtime``,
    ``executor``, ``target``, ...). Stored as a plain, JSON-safe dict so it
    round-trips through YAML untouched; reconstructed into a real
    :class:`~horus_runtime.core.task.base.BaseTask` for each clone via
    ``BaseTask.model_validate``. Must declare exactly one output (the one
    fanned into the gather task) and, when relevant, an input matching
    ``over.item_input``/``over.index_input``.
    """

    gather_task: str
    """Id of the pre-existing, user-authored task that fans clone outputs
    in."""

    gather_input: str
    """Input id on ``gather_task`` that receives the fan-in folder."""

    @property
    def _fanout_marker_id(self) -> str:
        """Id of this expander's internal wiring-only output marker."""
        return f"{self.id}{_FANOUT_SUFFIX}"

    @property
    def _source_marker_id(self) -> str | None:
        """
        The synthetic input used only to order collection enumeration.

        In artifact mode it is deliberately the root artifact's own id, so
        the ``artifact-<id>`` edge convention
        (:meth:`~horus_runtime.core.workflow.base.BaseWorkflow.
        _assert_edge_source_resolves`) resolves against it unchanged.
        """
        return self.over.source_artifact or self.over.source_output

    @property
    def _source_label(self) -> str:
        """How to name this map's collection in an error message."""
        if self.over.is_artifact:
            return _("root artifact '%(id)s'") % {
                "id": self.over.source_artifact
            }
        return f"{self.over.source_task}.{self.over.source_output}"

    def _shared_template_inputs(self) -> list[BaseArtifact]:
        """Return the template inputs that every clone receives unchanged."""
        template = self._build_clone(f"{self.id}[template]")
        per_clone = {self.over.item_input, self.over.index_input}
        return [
            artifact
            for artifact in template.inputs
            if artifact.id not in per_clone
        ]

    @model_validator(mode="after")
    def _ensure_fanout_marker(self) -> Self:
        """
        Append the internal wiring-only output marker if not already
        present (idempotent, so re-loading an already-dumped expander does
        not duplicate it).

        This output exists solely so :meth:`_run` can wire a real
        task-to-task edge from this expander to each clone — needed for a
        clone to fall inside the scheduler's trigger-reachable scope, which
        only follows edges between real tasks (see the module docstring).
        It is deliberately never written to disk, so :meth:`is_complete`
        stays ``False`` forever and the expander always re-runs.
        """
        marker_id = self._fanout_marker_id
        if not any(o.id == marker_id for o in self.outputs):
            self.outputs.append(
                FileArtifact(id=marker_id, path=Path(f"{marker_id}.marker"))
            )
        return self

    @model_validator(mode="after")
    def _ensure_template_ports(self) -> Self:
        """Expose the map source and every shared template input as ports.

        The marker is deliberately a file regardless of the collection's
        concrete kind: it is never transferred through the ordinary edge path.
        Shared inputs retain their exact declared artifact shape so normal
        workflow edge validation and transfer keep working for them.
        """
        marker_id = self._source_marker_id
        declared = {artifact.id for artifact in self.inputs}
        if marker_id is not None and marker_id not in declared:
            self.inputs.append(
                FileArtifact(
                    id=marker_id,
                    path=Path(f"{self.id}.over.marker"),
                )
            )
            declared.add(marker_id)

        for artifact in self._shared_template_inputs():
            if artifact.id == marker_id:
                raise MapConfigurationError(
                    _(
                        "Map task '%(id)s' uses '%(input)s' both as its "
                        "collection marker (the source output or root "
                        "artifact it fans out over) and as a shared "
                        "template input."
                    )
                    % {"id": self.id, "input": artifact.id}
                )
            if artifact.id not in declared:
                self.inputs.append(artifact.model_copy(deep=True))
                declared.add(artifact.id)
        return self

    async def is_complete(self) -> bool:
        """
        Always incomplete: the expander must re-run on every trigger to
        re-derive its deterministic clone set. On a resumed run this
        re-creates every clone; each clone's own :meth:`is_complete` then
        independently decides whether to skip it.
        """
        return False

    async def _reset(self) -> None:
        """
        Delete the (never-written) wiring marker, mirroring
        :meth:`~horus_builtin.task.horus_task.HorusTask._reset`. The
        clones/edges this expander creates are re-derived from scratch on
        every :meth:`_run` and reset independently as ordinary tasks.
        """
        store = ArtifactStore(self.target)
        for artifact in self.outputs:
            await store.delete(artifact)
        self.runs = 0

    async def _run(self) -> None:
        """
        Resolve the fan-out count, materialize each clone's slice/index,
        and atomically wire the clones plus fan-in edges into the live
        workflow. See the module docstring for the full algorithm.
        """
        wf = self.workflow
        if wf is None:
            raise MapConfigurationError(
                _("Map task '%(id)s' must run inside a workflow.")
                % {"id": self.id}
            )
        orchestrator = wf.orchestrator_target
        if orchestrator is None:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' requires workflow.orchestrator_"
                    "target to be set to materialize per-clone inputs."
                )
                % {"id": self.id}
            )

        self.runs += 1

        source_target, source_artifact = self._resolve_source(wf)
        items, count = await self._resolve_count(
            source_target, source_artifact
        )
        width = max(1, len(str(max(count - 1, 0))))

        run_root = wf.run_directory
        items_root = run_root / f"{self.id}{_ITEMS_DIR_SUFFIX}"
        gathered_root = run_root / f"{self.id}{_GATHERED_DIR_SUFFIX}"
        await orchestrator.mkdir(str(gathered_root))

        anchor_input = self.over.item_input or self.over.index_input
        if anchor_input is None:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' must set 'item_input' (collection "
                    "mode) or 'index_input' (range mode)."
                )
                % {"id": self.id}
            )

        clones: list[BaseTask] = []
        edges: list[WorkflowEdge] = []
        shared_inputs = {
            artifact.id for artifact in self._shared_template_inputs()
        }
        # These edges deliberately arrive as transfer=False: the expander only
        # needs their ordering relationship.  Their actual data transfer is
        # recreated below for each clone, whose inputs have their own paths.
        shared_edges = [
            edge
            for edge in wf.edges
            if edge.target == self.id and edge.target_input in shared_inputs
        ]

        for i in range(count):
            clone_id = f"{self.id}[{i:0{width}d}]"
            clone = self._build_clone(clone_id)

            if self.over.item_input is not None:
                item_path = await self._materialize_item(
                    source_target,
                    source_artifact,
                    orchestrator,
                    items_root,
                    i,
                    items,
                )
                self._set_input_path(clone, self.over.item_input, item_path)

            if self.over.index_input is not None:
                index_path = await self._materialize_index(
                    orchestrator, items_root, i
                )
                self._set_input_path(clone, self.over.index_input, index_path)

            await self._set_output_path(
                clone, gathered_root / str(i), orchestrator
            )

            clones.append(clone)
            edges.append(
                WorkflowEdge(
                    source=self.id,
                    source_output=self._fanout_marker_id,
                    target=clone.id,
                    target_input=anchor_input,
                    transfer=False,
                )
            )
            for shared_edge in shared_edges:
                edges.append(
                    shared_edge.model_copy(
                        update={"target": clone.id, "transfer": True}
                    )
                )
            edges.append(
                WorkflowEdge(
                    source=clone.id,
                    source_output=self._clone_output_id(clone),
                    target=self.gather_task,
                    target_input=self.gather_input,
                    transfer=False,
                )
            )

        self._set_gather_input_path(wf, gathered_root)

        if clones:
            wf.expand(tasks=clones, edges=edges)

        horus_logger.log.debug(
            _("Map task '%(id)s' expanded into %(n)d clone(s).")
            % {"id": self.id, "n": count}
        )

    def _resolve_source(
        self, wf: "BaseWorkflow"
    ) -> tuple[BaseTarget | None, BaseArtifact | None]:
        """
        Resolve the collection to enumerate, as the ``(target, artifact)``
        pair everything downstream actually needs: the target it can be read
        off, and the artifact naming it. ``(None, None)`` in range mode.

        A root artifact is read straight off the orchestrator, where
        ``_resolve_run_paths`` has already anchored it, rather than being
        transferred to the expander first. That is why the expander's
        incoming ``artifact-<id>`` edge is ordering-only: enumerating a
        collection must not copy it, and copying it here would move the same
        bytes a second time on the way to each clone.
        """
        if self.over.is_range:
            return None, None

        if self.over.is_artifact:
            if wf.orchestrator_target is None:
                raise MapConfigurationError(
                    _(
                        "Map task '%(id)s' requires workflow.orchestrator_"
                        "target to enumerate root artifact '%(artifact)s'."
                    )
                    % {"id": self.id, "artifact": self.over.source_artifact}
                )
            artifact = next(
                (a for a in wf.artifacts if a.id == self.over.source_artifact),
                None,
            )
            if artifact is None:
                raise MapConfigurationError(
                    _(
                        "Map task '%(id)s' references unknown root artifact "
                        "'%(artifact)s'."
                    )
                    % {"id": self.id, "artifact": self.over.source_artifact}
                )
            return wf.orchestrator_target, artifact

        source_task = next(
            (t for t in wf.tasks if t.id == self.over.source_task), None
        )
        if source_task is None:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' references unknown source task "
                    "'%(source_task)s'."
                )
                % {"id": self.id, "source_task": self.over.source_task}
            )
        source_artifact = next(
            (
                a
                for a in source_task.outputs
                if a.id == self.over.source_output
            ),
            None,
        )
        if source_artifact is None:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' references unknown output "
                    "'%(output)s' on source task '%(source_task)s'."
                )
                % {
                    "id": self.id,
                    "output": self.over.source_output,
                    "source_task": self.over.source_task,
                }
            )
        return source_task.target, source_artifact

    async def _resolve_count(
        self,
        source_target: BaseTarget | None,
        source_artifact: BaseArtifact | None,
    ) -> tuple[list[Any] | None, int]:
        """
        Resolve the fan-out count and, outside range mode, the ordered list
        of item descriptors: sorted child names for a
        :class:`.FolderArtifact` collection, or the raw JSON elements for a
        JSON-list collection. Returns ``(items, count)``; ``items`` is
        ``None`` in range mode.

        Collection and artifact mode share this whole body: by this point a
        collection is just an artifact plus the target it lives on.
        """
        if self.over.is_range:
            assert self.over.range is not None
            return None, self.over.range

        assert source_target is not None
        assert source_artifact is not None

        store = ArtifactStore(source_target)
        if not await store.exists(source_artifact):
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' source collection %(source)s does "
                    "not exist yet."
                )
                % {"id": self.id, "source": self._source_label}
            )

        target_path = source_target.path_on_target(source_artifact)
        if isinstance(source_artifact, FolderArtifact):
            entries = await source_target.list_dir(target_path)
            names = sorted(entry.name for entry in entries)
            return names, len(names)

        raw = await source_target.get_file(target_path)
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' source collection %(source)s must be "
                    "a FolderArtifact or a JSON list."
                )
                % {"id": self.id, "source": self._source_label}
            )
        return parsed, len(parsed)

    async def _materialize_item(
        self,
        source_target: BaseTarget | None,
        source_artifact: BaseArtifact | None,
        orchestrator: BaseTarget,
        items_root: Path,
        i: int,
        items: list[Any] | None,
    ) -> Path:
        """
        Materialize the i-th item on the orchestrator's filesystem and
        return its absolute path: a copy of the i-th child directory for a
        folder collection, or a small JSON file holding the i-th element
        for a JSON-list collection.
        """
        assert source_target is not None
        assert source_artifact is not None
        assert items is not None

        if isinstance(source_artifact, FolderArtifact):
            base = source_target.path_on_target(source_artifact)
            child_path = f"{base}/{items[i]}"
            dest = items_root / str(i)
            await self._copy_folder(
                source_target, child_path, orchestrator, dest
            )
            return dest

        dest = items_root / f"{i}.json"
        await orchestrator.put_file(
            json.dumps(items[i]).encode("utf-8"), str(dest)
        )
        return dest

    @staticmethod
    async def _materialize_index(
        orchestrator: BaseTarget, items_root: Path, i: int
    ) -> Path:
        """Write loop index *i* as a small JSON file and return its
        absolute local path.
        """
        dest = items_root / f"{i}.index.json"
        await orchestrator.put_file(json.dumps(i).encode("utf-8"), str(dest))
        return dest

    @staticmethod
    async def _copy_folder(
        src_target: BaseTarget,
        src_path: str,
        dst_target: BaseTarget,
        dst_path: Path,
    ) -> None:
        """
        Copy the directory at *src_path* on *src_target* into the local
        directory *dst_path* on *dst_target*.

        Same-filesystem source/destination pairs are copied directly;
        otherwise the directory is packaged into a tarball on the source,
        fetched, and extracted locally, so this works for a remote source
        target too.
        """
        if src_target.location_id == dst_target.location_id:
            shutil.rmtree(dst_path, ignore_errors=True)
            shutil.copytree(src_path, dst_path)
            return

        pkg_name = f"horus-map-slice-{uuid.uuid4().hex[:8]}.tar.gz"
        pkg_path = f"{src_target.resolved_working_directory}/{pkg_name}"
        pack_cmd = (
            f"tar czf {shlex.quote(pkg_path)} -C {shlex.quote(src_path)} ."
        )
        proc = await src_target.run_command_sync(pack_cmd)
        rc = await proc.wait()
        if rc != 0:
            raise MapConfigurationError(
                _("Failed to package map slice at '%(path)s'.")
                % {"path": src_path}
            )
        try:
            data = await src_target.get_file(pkg_path)
        finally:
            await src_target.remove(pkg_path)

        dst_path.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=BytesIO(data)) as tf:
            tf.extractall(dst_path)

    def _build_clone(self, clone_id: str) -> BaseTask:
        """Reconstruct a fresh, independent clone task from ``template``."""
        data: dict[str, Any] = {**self.template}
        data.setdefault("kind", "horus_task")
        data["id"] = clone_id
        data["name"] = clone_id
        # Propagate a forced re-run (e.g. CLI ``--no-skip-all``/``--no-skip``,
        # which flips the expander's own ``skip_if_complete``) onto each clone.
        if not self.skip_if_complete:
            data["skip_if_complete"] = False
        clone = BaseTask.model_validate(data)
        clone.target = clone.target.model_copy(deep=True)
        return clone

    @staticmethod
    def _pin_path(artifact: BaseArtifact, path: Path) -> None:
        """
        Point *artifact* at the absolute *path* and mark it as already
        anchored, so the workflow's run-directory anchoring
        (``BaseWorkflow._anchor_task``, run by ``expand()`` for each new
        clone) leaves it untouched.
        """
        artifact.path = path
        artifact.declared_path = path
        artifact.pinned = True

    def _set_input_path(
        self, clone: BaseTask, input_id: str, path: Path
    ) -> None:
        """Pin *clone*'s declared input *input_id* at *path*."""
        artifact = next((a for a in clone.inputs if a.id == input_id), None)
        if artifact is None:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' template does not declare an "
                    "input '%(input_id)s'."
                )
                % {"id": self.id, "input_id": input_id}
            )
        self._pin_path(artifact, path)

    async def _set_output_path(
        self, clone: BaseTask, slot_dir: Path, orchestrator: BaseTarget
    ) -> None:
        """
        Pin *clone*'s (sole) declared output under its ``{i}/`` slot dir.

        A :class:`.FolderArtifact` output already names a directory, so it
        is pinned straight at *slot_dir* (the gather task then sees
        ``{i}/<whatever the clone wrote>``). Any other output keeps its
        declared filename, pinned at ``slot_dir / <filename>``; since
        nothing else creates *slot_dir* for a bare file output, it is
        created here first (only in this branch -- pre-creating it for a
        folder output would make the clone look already-complete and skip
        it on resume).

        A :class:`~horus_builtin.workflow.subworkflow.expander.
        SubworkflowExpander` clone is a third case: its sole declared
        output is always a never-written ``FileArtifact`` port placeholder,
        regardless of what the real inner producer's output kind is. Its
        own ``_run`` re-points that real producer at wherever the
        placeholder got pinned (see ``SubworkflowExpander._pin_out_port``),
        so it is pinned straight at *slot_dir* like a folder output,
        leaving that re-pointing free to hand the real producer's own
        declared filename onward.
        """
        if len(clone.outputs) != 1:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' template must declare exactly "
                    "one output to feed the gather task."
                )
                % {"id": self.id}
            )
        # Imported here, as in BaseWorkflow.sub, to avoid a
        # base.py <-> map.py <-> expander.py cycle at module load time.
        from horus_builtin.workflow.subworkflow.expander import (  # noqa: PLC0415
            SubworkflowExpander,
        )

        artifact = clone.outputs[0]
        if isinstance(artifact, FolderArtifact) or isinstance(
            clone, SubworkflowExpander
        ):
            self._pin_path(artifact, slot_dir)
        else:
            await orchestrator.mkdir(str(slot_dir))
            self._pin_path(artifact, slot_dir / artifact.path.name)

    @staticmethod
    def _clone_output_id(clone: BaseTask) -> str:
        """The id of *clone*'s sole declared output."""
        return clone.outputs[0].id

    def _set_gather_input_path(
        self, wf: "BaseWorkflow", gathered_root: Path
    ) -> None:
        """
        Point the gather task's designated input at the shared
        ``{id}.gathered/`` folder every clone writes its own ``{i}/``
        subdirectory under.
        """
        gather = next((t for t in wf.tasks if t.id == self.gather_task), None)
        if gather is None:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' references unknown gather task "
                    "'%(gather_task)s'."
                )
                % {"id": self.id, "gather_task": self.gather_task}
            )
        artifact = next(
            (a for a in gather.inputs if a.id == self.gather_input), None
        )
        if artifact is None:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' gather task '%(gather_task)s' "
                    "does not declare an input '%(gather_input)s'."
                )
                % {
                    "id": self.id,
                    "gather_task": self.gather_task,
                    "gather_input": self.gather_input,
                }
            )
        self._pin_path(artifact, gathered_root)


def lower_map_entry(
    entry: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Lower one raw YAML task-dict carrying a ``map:`` block into a
    ``map_expander`` task-dict plus the construction-time edges it needs.

    In collection mode this is a single ordering edge from
    ``over.source_task`` to the expander (``transfer=False``: the expander
    reads the collection directly off the source task's own target rather
    than through the generic transfer path, so no bytes need to move just
    to enumerate/count it). Range mode needs no construction-time edge.

    Args:
        entry: The raw task dict as parsed from YAML, carrying ``id`` and
            a ``map`` key (``over``/``range``, ``template``, ``gather``).

    Returns:
        ``(expander_dict, edges)`` — a ``kind: map_expander`` task dict
        ready for ``BaseTask.model_validate``, and any edges (as plain
        dicts) that must accompany it into the workflow's ``edges`` list.

    Raises:
        MapConfigurationError: If *entry* is missing the ``id`` key, or its
            ``map`` block is missing ``template`` or ``gather``, or
            ``gather`` is missing ``task`` or ``input``.
    """
    task_id = entry.get("id")
    if task_id is None:
        raise MapConfigurationError(
            _("A task with a 'map' block is missing required key 'id'.")
        )
    map_block = entry["map"]

    if "template" not in map_block:
        raise MapConfigurationError(
            _("Map task '%(id)s' is missing required key 'map.template'.")
            % {"id": task_id}
        )
    if "gather" not in map_block:
        raise MapConfigurationError(
            _("Map task '%(id)s' is missing required key 'map.gather'.")
            % {"id": task_id}
        )

    over_block = map_block.get("over") or {}
    range_value = map_block.get("range")
    index_input = map_block.get("index_input", over_block.get("index_input"))

    over: dict[str, Any] = {
        "source_task": over_block.get("source_task"),
        "source_output": over_block.get("source_output"),
        "source_artifact": over_block.get("source_artifact"),
        "item_input": over_block.get("item_input"),
        "index_input": index_input,
        "range": range_value,
    }

    gather_block = map_block["gather"]
    if not isinstance(gather_block, dict) or "task" not in gather_block:
        raise MapConfigurationError(
            _("Map task '%(id)s' 'map.gather' is missing required key 'task'.")
            % {"id": task_id}
        )
    if "input" not in gather_block:
        raise MapConfigurationError(
            _(
                "Map task '%(id)s' 'map.gather' is missing required "
                "key 'input'."
            )
            % {"id": task_id}
        )

    expander: dict[str, Any] = {
        "kind": "map_expander",
        "id": task_id,
        "name": entry.get("name") or task_id,
        "description": entry.get("description", ""),
        "over": over,
        "template": map_block["template"],
        "gather_task": gather_block["task"],
        "gather_input": gather_block["input"],
        "inputs": [],
    }
    if entry.get("target") is not None:
        expander["target"] = entry["target"]

    edges: list[dict[str, Any]] = []
    if range_value is None:
        # Both collection and artifact mode need the same ordering edge into
        # the same marker port; only the id and the endpoint naming it differ
        # (a root artifact sources via the ``artifact-<id>`` convention).
        marker_id = over["source_artifact"] or over["source_output"]
        expander["inputs"] = [
            {
                "kind": "file",
                "id": marker_id,
                "path": f"{task_id}.over.marker",
            }
        ]
        edges.append(
            {
                "source": (
                    f"artifact-{marker_id}"
                    if over["source_artifact"]
                    else over["source_task"]
                ),
                "source_output": marker_id,
                "target": task_id,
                "target_input": marker_id,
                "transfer": False,
            }
        )

    # Gate the gather task behind the expander. Without it the gather task is
    # ready from the start (its only real dependencies -- the clones -- do not
    # exist until the expander runs), so the scheduler dispatches it alongside
    # the expander and its not-yet-pinned fan-in input is mistaken for a root
    # input and fetched from the orchestrator target.
    edges.append(
        {
            "source": task_id,
            "source_output": f"{task_id}{_FANOUT_SUFFIX}",
            "target": gather_block["task"],
            "target_input": gather_block["input"],
            "transfer": False,
        }
    )

    return expander, edges


def map_task(
    wf: "BaseWorkflow",
    *,
    id: str,
    template: BaseTask,
    gather: tuple[str, str],
    over: tuple[str, str, str] | None = None,
    over_artifact: tuple[str, str] | None = None,
    range: int | None = None,
    index_input: str | None = None,
    name: str | None = None,
    target: BaseTarget | None = None,
) -> MapExpander:
    """
    Append a declarative map (fan-out/fan-in) task to *wf*.

    Mirrors :meth:`~horus_builtin.task.function.FunctionTask.task`'s
    ergonomics for the map construct: builds and appends the same
    :class:`MapExpander` (plus its construction-time wiring edge, in
    collection mode) that YAML's ``map:`` block lowers to via
    :func:`lower_map_entry`, so the two authoring paths produce
    structurally equivalent tasks/edges.

    Args:
        wf: The workflow to append to.
        id: Id of the map task (and id-prefix of each clone).
        template: The per-clone task, in full. Its own ``id``/``name`` are
            ignored — every clone gets its own deterministic id. Must
            declare exactly one output (fanned into ``gather``) and, when
            relevant, an input matching ``over``'s item / ``index_input``.
        gather: ``(gather_task_id, gather_input_id)`` of the pre-existing
            task that fans clone outputs in.
        over: ``(source_task_id, source_output_id, item_input_id)`` for
            collection mode. Mutually exclusive with *over_artifact* and
            *range*.
        over_artifact: ``(root_artifact_id, item_input_id)`` for artifact
            mode. Mutually exclusive with *over* and *range*.
        range: Clone count for range mode. Mutually exclusive with the two
            *over* arguments.
        index_input: Input id on *template* that receives each clone's
            integer index. Required in range mode; optional in collection
            mode (alongside *over*'s item).
        name: Display name; defaults to *id*.
        target: Target the expander itself runs on; defaults to
            ``LocalTarget()``.

    Returns:
        The appended :class:`MapExpander`.

    Raises:
        MapConfigurationError: Unless exactly one of *over*,
            *over_artifact* and *range* is given.
    """
    if sum(arg is not None for arg in (over, over_artifact, range)) != 1:
        raise MapConfigurationError(
            _(
                "wf.map(id='%(id)s', ...) requires exactly one of 'over', "
                "'over_artifact' or 'range'."
            )
            % {"id": id}
        )

    gather_task, gather_input = gather
    template_data = template.model_dump(mode="json")

    inputs: list[BaseArtifact] = []
    edges: list[WorkflowEdge] = []

    if over is not None or over_artifact is not None:
        if over is not None:
            source_task, marker_id, item_input = over
            map_over = MapOver(
                source_task=source_task,
                source_output=marker_id,
                item_input=item_input,
                index_input=index_input,
            )
            source_id = source_task
        else:
            assert over_artifact is not None
            marker_id, item_input = over_artifact
            map_over = MapOver(
                source_artifact=marker_id,
                item_input=item_input,
                index_input=index_input,
            )
            # Root artifacts are sourced through the `artifact-<id>`
            # convention rather than by a producing task's id.
            source_id = f"artifact-{marker_id}"
        inputs.append(
            FileArtifact(id=marker_id, path=Path(f"{id}.over.marker"))
        )
        edges.append(
            WorkflowEdge(
                source=source_id,
                source_output=marker_id,
                target=id,
                target_input=marker_id,
                transfer=False,
            )
        )
    else:
        map_over = MapOver(range=range, index_input=index_input)

    kwargs: dict[str, Any] = {
        "id": id,
        "name": name or id,
        "over": map_over,
        "template": template_data,
        "gather_task": gather_task,
        "gather_input": gather_input,
        "inputs": inputs,
    }
    if target is not None:
        kwargs["target"] = target

    expander = MapExpander(**kwargs)

    # Same ordering edge lower_map_entry emits; see its comment.
    edges.append(
        WorkflowEdge(
            source=id,
            source_output=expander._fanout_marker_id,  # noqa: SLF001
            target=gather_task,
            target_input=gather_input,
            transfer=False,
        )
    )

    wf.tasks.append(expander)
    wf.edges.extend(edges)
    return expander
