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
horus_map: a task that wraps another task and runs it once per item of a
collection, fanning the clones' outputs into a single folder output.

Optionally the collection itself is produced by a fan-out transform body
(``runtime``/``executor``, used only when ``fan_out`` is set), and the
clones' per-slot outputs are folded into a single output by a gather
transform instead of being fanned into a folder.
"""

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import Field, field_serializer, model_validator
from pydantic_core.core_schema import SerializerFunctionWrapHandler

from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.task.horus_task import HorusTask, TaskFingerprint
from horus_builtin.workflow.declared_paths import _restore_declared_paths
from horus_builtin.workflow.scheduler import TargetPool, execute_task
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.iterable import IterableArtifact
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.workflow.base import EdgeSource
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.core.workflow.exceptions import WorkflowError
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger


class MapConfigurationError(WorkflowError):
    """Raised when a ``horus_map`` task is misconfigured."""


_SLOTS_ARTIFACT_ID = "slots"
"""
Reserved input id for the internal artifact carrying the gathered clones'
outputs; an author-declared input may not use it when ``gather`` is set.
"""


class MapTask(HorusTask):
    """
    Wraps :attr:`task` and runs one clone of it per item of the collection,
    then fans every clone's outputs into this task's sole folder output.

    The collection is :attr:`over` when that input is itself iterable
    (see :class:`~horus_runtime.core.artifact.iterable.IterableArtifact`);
    otherwise :attr:`fan_out` must name an iterable intermediate that the
    fan-out transform body (:attr:`runtime` run through :attr:`executor`)
    produces first. With :attr:`gather`, the clones' per-slot outputs are
    re-rooted under an internal ``slots`` folder and folded into the single
    declared output by a second transform instead.
    """

    kind: str = "horus_map"
    kind_name: ClassVar[str] = "Map"
    kind_description: ClassVar[str] = _(
        "Runs a wrapped task once per item of a collection, fanning the "
        "clones' outputs into a single folder output."
    )

    runtime: BaseRuntime = Field(
        default_factory=lambda: CommandRuntime(command="true")
    )
    """
    Fan-out transform body: executed through :attr:`executor` before
    iteration to materialize :attr:`fan_out`. Unused unless :attr:`fan_out`
    is set; exists with inert defaults otherwise so ``BaseTask``'s required
    fields are satisfied.
    """

    executor: BaseExecutor = Field(default_factory=ShellExecutor)

    task: BaseTask
    """
    The wrapped, per-clone task.
    """

    over: str
    """
    Id of one of *this* task's own :attr:`~BaseTask.inputs` carrying the
    collection to fan out over. Must be iterable unless :attr:`fan_out`
    names a transform-produced collection instead.
    """

    item_input: str
    """
    Id of the input on :attr:`task` that receives each item.
    """

    max_concurrency: int | None = None
    """Upper bound on clones dispatched at once; ``None`` means unbounded."""

    fan_out: BaseArtifact | None = None
    """
    Iterable intermediate the transform body produces, e.g. a JSON list
    written by a command; its items drive the fan-out. When set, :attr:`over`
    does not have to be iterable, and :attr:`runtime` stops being inert: it
    is executed through :attr:`executor` before iteration.

    Must be an :class:`~horus_runtime.core.artifact.iterable.
    IterableArtifact` instance whose id does not collide with any input or
    output id.
    """

    gather: BaseRuntime | None = None
    """
    Gather transform: folds the clones' outputs, re-rooted under an internal
    ``slots`` folder, into this task's single declared output. Run through
    :attr:`executor` after all clones finish; ``$slots`` resolves to the
    slots folder and each ``$<output id>`` of this task resolves as usual in
    the transform's template.

    Because ``slots`` becomes one of this task's inputs for the gather run,
    no author-declared input may use that reserved id while :attr:`gather`
    is set. With :attr:`gather`, exactly one output of any kind is allowed
    (without it, the sole output must still be a folder).
    """

    @model_validator(mode="before")
    @classmethod
    def _default_task_identity(cls, data: Any) -> Any:
        """
        Fill ``task.id``/``name`` when authored without one; nothing
        ever reads them (clone ids are always ``f"{map.id}[{slot}]"``).
        """
        if not isinstance(data, dict):
            return data
        task = data.get("task")
        if isinstance(task, dict) and "id" not in task:
            task = {**task, "id": f"{data.get('id', 'map')}.body"}
            task.setdefault("name", task["id"])
            data = {**data, "task": task}
        return data

    @field_serializer("task", mode="wrap")
    def _dump_task(
        self, task: BaseTask, handler: SerializerFunctionWrapHandler
    ) -> Any:
        """
        Dump ``task`` with declared (pre-resolution) artifact paths, not the
        eagerly CWD-resolved ones ``BaseArtifact`` carries at runtime, so a
        ``to_yaml``/``from_yaml`` round trip keeps relative paths relative
        instead of baking in this process's CWD.
        """
        document = handler(task)
        _restore_declared_paths(
            document.get("inputs") or [], list(task.inputs)
        )
        _restore_declared_paths(
            document.get("outputs") or [], list(task.outputs)
        )
        return document

    @model_validator(mode="after")
    def _check_ports(self) -> Self:
        """
        Validate wiring, then adopt each of ``task``'s other inputs as
        one of this task's own (an author-declared one of the same id wins).

        Also validates the optional transforms: ``fan_out`` must be iterable
        and not collide with any port, ``gather`` reserves the ``slots``
        input id and relaxes the folder-output rule to any single output,
        and both transforms must pair their runtime with an executor that
        can actually run it.
        """
        over_artifact = next(
            (a for a in self.inputs if a.id == self.over), None
        )
        if over_artifact is None:
            raise MapConfigurationError(
                _("Map task '%(id)s' 'over' names unknown input '%(over)s'.")
                % {"id": self.id, "over": self.over}
            )
        if self.fan_out is None and not isinstance(
            over_artifact, IterableArtifact
        ):
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' 'over' input '%(over)s' has kind "
                    "'%(kind)s', which cannot be iterated. Use an iterable "
                    "input kind (e.g. folder or json), or set 'fan_out' so "
                    "the transform body produces the collection."
                )
                % {
                    "id": self.id,
                    "over": self.over,
                    "kind": over_artifact.kind,
                }
            )
        if not any(a.id == self.item_input for a in self.task.inputs):
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' 'item_input' names unknown input "
                    "'%(item_input)s' on the wrapped task."
                )
                % {"id": self.id, "item_input": self.item_input}
            )

        # Adopt the wrapped task's other inputs as this task's own, so they
        # get materialized on the orchestrator's target and can be copied to
        # each clone's target. Skip the item_input (it gets a fresh copy per
        # item) and any input already declared on this task (the author may
        # have wired it to a different artifact than the wrapped task's).
        own_input_ids = {a.id for a in self.inputs}
        for inner in self.task.inputs:
            if inner.id == self.item_input or inner.id in own_input_ids:
                continue
            self.inputs.append(inner.model_copy(deep=True))

        self._validate_outputs()
        self._validate_fan_out()
        self._validate_gather()
        self._validate_transform_runtimes()

        return self

    def _validate_outputs(self) -> None:
        """
        Without :attr:`gather`, exactly one FolderArtifact output is
        required; with it, exactly one output of any kind (the transform
        folds the slots folder into whatever the author declared).
        """
        if self.gather is None:
            if len(self.outputs) != 1 or not isinstance(
                self.outputs[0], FolderArtifact
            ):
                raise MapConfigurationError(
                    _(
                        "Map task '%(id)s' must declare exactly one "
                        "FolderArtifact output."
                    )
                    % {"id": self.id}
                )
        elif len(self.outputs) != 1:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' with 'gather' must declare exactly "
                    "one output, of any kind."
                )
                % {"id": self.id}
            )

    def _validate_fan_out(self) -> None:
        """
        The intermediate must itself be iterable and must not shadow any
        input or output id (the gather run would otherwise resolve ``$id``
        ambiguously between them).
        """
        if self.fan_out is None:
            return

        if not isinstance(self.fan_out, IterableArtifact):
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' 'fan_out' artifact '%(artifact)s' "
                    "has kind '%(kind)s', which cannot be iterated; use an "
                    "iterable kind (e.g. folder or json)."
                )
                % {
                    "id": self.id,
                    "artifact": self.fan_out.id,
                    "kind": self.fan_out.kind,
                }
            )
        port_ids = {a.id for a in (*self.inputs, *self.outputs)}
        if self.fan_out.id in port_ids:
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' 'fan_out' id '%(artifact)s' collides "
                    "with an input or output id."
                )
                % {"id": self.id, "artifact": self.fan_out.id}
            )

    def _validate_gather(self) -> None:
        """
        No input may use the reserved ``slots`` id while :attr:`gather` is
        set: the gather run injects an internal artifact carrying the
        clones' outputs under that exact id.
        """
        if self.gather is None:
            return

        if any(a.id == _SLOTS_ARTIFACT_ID for a in self.inputs):
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' input id 'slots' is reserved while "
                    "'gather' is set: it names the internal artifact "
                    "carrying the clones' outputs."
                )
                % {"id": self.id}
            )

    def _validate_transform_runtimes(self) -> None:
        """
        Each configured transform runtime must satisfy ``executor.runtimes``
        (the same ClassVar check the backend's compatibility listing uses),
        since one shared executor serves both transforms.
        """
        expected = [r.__name__ for r in self.executor.runtimes]
        if self.fan_out is not None and not isinstance(
            self.runtime, self.executor.runtimes
        ):
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' fan-out runtime '%(runtime)s' is "
                    "not compatible with executor '%(executor)s'. Expected "
                    "one of: %(expected)s"
                )
                % {
                    "id": self.id,
                    "runtime": type(self.runtime).__name__,
                    "executor": type(self.executor).__name__,
                    "expected": expected,
                }
            )
        if self.gather is not None and not isinstance(
            self.gather, self.executor.runtimes
        ):
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' gather runtime '%(runtime)s' is not "
                    "compatible with executor '%(executor)s'. Expected one "
                    "of: %(expected)s"
                )
                % {
                    "id": self.id,
                    "runtime": type(self.gather).__name__,
                    "executor": type(self.executor).__name__,
                    "expected": expected,
                }
            )

    async def _execute(self) -> None:
        """
        Run the fan-out transform when configured, build one clone per item
        of the collection, and run them all concurrently through
        :func:`~horus_builtin.workflow.scheduler.execute_task`, then
        aggregate their side artifacts onto this task. With :attr:`gather`,
        a second transform run folds the clones' re-rooted outputs into the
        declared output afterwards.
        """
        wf = self.workflow
        if wf is None:
            raise MapConfigurationError(
                _("Map task '%(id)s' must run inside a workflow.")
                % {"id": self.id}
            )

        fan_out = self.fan_out
        if fan_out is not None:
            # Anchor an authored relative path under this invocation's own
            # working directory so the transform body writes (and iteration
            # reads) a deterministic, per-invocation location. declared_path
            # stays untouched: dumps and fingerprints restore from it.
            declared = fan_out.declared_path
            if declared is not None and not declared.is_absolute():
                fan_out.path = Path(self.working_dir) / declared
            # A fresh side-artifacts list per view: model_copy(update=...)
            # would otherwise share ours and every produced side artifact
            # could land twice once we extend below.
            view = self.model_copy(
                update={"outputs": [fan_out], "side_artifacts": []}
            )
            await self.executor.execute(view)
            self.side_artifacts.extend(view.side_artifacts)

        gather = self.gather
        slots_art: FolderArtifact | None = None
        if gather is not None:
            # Internal re-root for the clones' outputs; folded into the
            # declared output by the gather transform below.
            slots_art = FolderArtifact(
                id=_SLOTS_ARTIFACT_ID,
                path=Path(self.working_dir) / _SLOTS_ARTIFACT_ID,
            )
            await self.target.mkdir(self.target.path_on_target(slots_art))
        else:
            output = self.outputs[0]
            await self.target.mkdir(self.target.path_on_target(output))

        root_artifact = slots_art if slots_art is not None else self.outputs[0]
        slot_root_base = Path(self.target.path_on_target(root_artifact))

        slots = await self._slots()
        clones = [
            self._clone(slot, item, slot_root_base) for slot, item in slots
        ]
        # Registers the clones as ordinary DAG tasks, each ordered after
        # this one by an artifact-less ordering edge (WorkflowEdge with no
        # source_output/target_input, forced transfer=False) -- the same
        # pattern LoopController uses for its body/checker tasks. This is
        # what makes clones visible to anything that reads workflow.tasks
        # (a live dashboard, to_yaml/model_dump, a resumed-from-snapshot
        # run) and shows them nested under this task in a dependency view;
        # expand()'s supersede semantics keep only the current run's clones
        # on a re-run. execute_task's terminal-status no-op (see
        # scheduler.py) is what stops the outer ready-set loop from
        # redispatching a clone once it notices it ready behind this task.
        wf.expand(
            tasks=clones,
            edges=[
                WorkflowEdge(source=self.id, target=clone.id)
                for clone in clones
            ],
        )

        # Every clone input is already materialized on `self.target` (either
        # the item, or a copy of one of this task's own already-transferred
        # inputs), so it doubles as its own transfer origin: a co-located
        # clone target no-ops through BaseTransferStrategy.transfer's
        # same-location_id shortcut, a non-co-located one gets a real
        # transfer via the registered strategy. Nothing needs `pinned`.
        source_map: dict[tuple[str, str], EdgeSource] = {
            (clone.id, artifact.id): EdgeSource(self.target, artifact)
            for clone in clones
            for artifact in clone.inputs
        }

        pool = TargetPool(self.max_concurrency)
        async with asyncio.TaskGroup() as tg:
            for clone in clones:
                tg.create_task(
                    execute_task(
                        wf,
                        clone,
                        source_map=source_map,
                        pool=pool,
                        placement=wf.placement,
                    )
                )

        for clone in clones:
            self.side_artifacts.extend(clone.side_artifacts)

        horus_logger.log.debug(
            _("Map task '%(id)s' ran %(n)d clone(s).")
            % {"id": self.id, "n": len(clones)}
        )

        if gather is not None and slots_art is not None:
            gview = self.model_copy(
                update={
                    "runtime": gather,
                    "inputs": [*self.inputs, slots_art],
                    "outputs": list(self.outputs),
                    "side_artifacts": [],
                }
            )
            await self.executor.execute(gview)
            self.side_artifacts.append(slots_art)
            self.side_artifacts.extend(gview.side_artifacts)

    async def _slots(self) -> list[tuple[str, BaseArtifact]]:
        """
        Resolve the collection (:attr:`fan_out` when set, otherwise the
        :attr:`over` input), enumerate it through
        :meth:`~horus_runtime.core.artifact.iterable.IterableArtifact.items`
        on this task's target, and return one ``(slot_name,
        item_artifact)`` pair per element in that deterministic order.

        Items carrying a pre-materialized on-target *path* are pointed at it
        directly (zero copying); items carrying a *value* are serialized
        onto the target under this task's working directory via
        :meth:`_item_from_value`.
        """
        collection = self.fan_out
        if collection is None:
            collection = next(a for a in self.inputs if a.id == self.over)
        # _check_ports guarantees the collection iterates.
        assert isinstance(collection, IterableArtifact)

        item_template = next(
            a for a in self.task.inputs if a.id == self.item_input
        )

        slots: list[tuple[str, BaseArtifact]] = []
        for artifact_item in await collection.items(self.target):
            if artifact_item.path is not None:
                slots.append(
                    (
                        artifact_item.slot,
                        self._item_at(item_template, Path(artifact_item.path)),
                    )
                )
            else:
                slots.append(
                    (
                        artifact_item.slot,
                        await self._item_from_value(
                            item_template,
                            artifact_item.slot,
                            artifact_item.value,
                        ),
                    )
                )
        return slots

    @staticmethod
    def _item_at(template: BaseArtifact, path: Path) -> BaseArtifact:
        """A fresh copy of *template*, repointed at the absolute *path*."""
        item = template.model_copy(deep=True)
        item.path = path
        item.declared_path = path
        return item

    async def _item_from_value(
        self, template: BaseArtifact, slot: str, value: Any
    ) -> BaseArtifact:
        """
        Materialize one *value* item for *slot* on this task's **target**.

        The value is serialized through *template*'s own ``write`` into a
        local temporary file, uploaded to the target-side item path via
        ``self.target.put_file`` (never by writing the target-side path with
        local filesystem calls), and the returned fresh copy of *template* is
        repointed at that target-side path. The slot's item lands under this
        task's working directory as ``items/<slot><suffix>``, the suffix
        taken from *template*'s declared path.
        """
        declared = template.declared_path or template.path
        suffix = Path(declared).suffix
        item_path = Path(self.working_dir) / "items" / f"{slot}{suffix}"

        with tempfile.TemporaryDirectory() as staging_dir:
            staged_path = Path(staging_dir) / f"{slot}{suffix}"
            staged = template.model_copy(deep=True)
            staged.path = staged_path
            staged.write(value)
            await self.target.put_file(staged_path, item_path.as_posix())

        item = template.model_copy(deep=True)
        item.path = item_path
        item.declared_path = item_path
        return item

    def _clone(
        self, slot: str, item: BaseArtifact, slot_root_base: Path
    ) -> BaseTask:
        """
        Build one independent clone of :attr:`task` for *slot*, bound to
        *item*, with every other input shared verbatim from this task's own
        inputs and every relative output path re-rooted under
        *slot_root_base* (this task's folder output, or the internal slots
        folder when :attr:`gather` is set).
        """
        wf = self.workflow
        assert wf is not None  # _execute already checked this

        clone = self.task.model_copy(deep=True)
        clone.id = f"{self.id}[{slot}]"
        clone.name = clone.id
        clone.target = clone.target.model_copy(deep=True)
        # Propagate a forced re-run (e.g. CLI --no-skip-all/--no-skip,
        # which flips this task's own skip_if_complete) onto each clone.
        if not self.skip_if_complete:
            clone.skip_if_complete = False

        own_by_id = {a.id: a for a in self.inputs}
        clone.inputs = [
            item
            if inner.id == self.item_input
            else self._pinned_copy(own_by_id[inner.id])
            for inner in clone.inputs
        ]

        slot_root = slot_root_base / slot
        for artifact in clone.outputs:
            declared = artifact.declared_path
            if declared is None or declared.is_absolute():
                continue
            artifact.path = slot_root / declared
            artifact.declared_path = artifact.path

        # Anchors the clone's runtime/executor local paths and gives a
        # co-located clone target the orchestrator's working directory,
        # exactly as BaseWorkflow.expand does for every DAG-registered task.
        # Every artifact path is already absolute at this point, so it is a
        # no-op for those.
        wf._anchor_task(clone)  # noqa: SLF001
        return clone

    @staticmethod
    def _pinned_copy(artifact: BaseArtifact) -> BaseArtifact:
        """
        A copy of *artifact* with its *current* path frozen as its declared
        path too.

        This task's own inputs are already materialized on ``self.target``
        by the time a clone is built (the generic transfer step already ran
        for this task), but their ``declared_path`` still holds whatever
        relative value they were authored with. Freezing the current
        (absolute, already-correct) path onto ``declared_path`` on the copy
        stops :meth:`~horus_runtime.core.workflow.base.BaseWorkflow.
        _anchor_task` from re-deriving -- and clobbering -- it from that
        stale relative value.
        """
        copy = artifact.model_copy(deep=True)
        copy.declared_path = copy.path
        return copy

    async def _fingerprint(self) -> TaskFingerprint:
        """
        Everything :meth:`HorusTask._fingerprint` covers, plus a hash of the
        wrapped :attr:`task`'s own configuration and of both optional
        transforms (:attr:`fan_out` declaration, :attr:`gather` runtime), so
        editing the inner command or either transform invalidates this map's
        memoization too.

        Uses the same declared-path restoration as :meth:`_dump_task`
        (rather than a raw ``self.task.model_dump()``), so the hash reflects
        the task's *authored* configuration and stays stable across runs
        launched from different working directories.
        """
        base = await super()._fingerprint()
        task_doc = self.task.model_dump(mode="json")
        _restore_declared_paths(
            task_doc.get("inputs") or [], list(self.task.inputs)
        )
        _restore_declared_paths(
            task_doc.get("outputs") or [], list(self.task.outputs)
        )

        fan_out_doc: Any = None
        if self.fan_out is not None:
            fan_out_doc = self.fan_out.model_dump(mode="json")
            _restore_declared_paths([fan_out_doc], [self.fan_out])

        config = json.dumps(
            {
                "runtime": self.runtime.model_dump(mode="json"),
                "executor": self.executor.model_dump(mode="json"),
                "task": task_doc,
                "fan_out": fan_out_doc,
                "gather": (
                    self.gather.model_dump(mode="json")
                    if self.gather is not None
                    else None
                ),
            },
            sort_keys=True,
        )
        config_hash = hashlib.sha256(config.encode()).hexdigest()
        return TaskFingerprint(inputs=base.inputs, config_hash=config_hash)
