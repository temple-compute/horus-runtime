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
"""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import Field, field_serializer, model_validator
from pydantic_core.core_schema import SerializerFunctionWrapHandler

from horus_builtin.artifact.folder import FolderArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.task.horus_task import HorusTask, TaskFingerprint
from horus_builtin.workflow.scheduler import TargetPool, execute_task
from horus_runtime.core.artifact.base import BaseArtifact
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


def _restore_declared_paths(
    entries: list[dict[str, Any]], artifacts: list[BaseArtifact]
) -> None:
    """
    Undo the eager CWD resolution ``BaseArtifact`` applies at construction,
    so a dumped document keeps the relative paths its author wrote. See
    :meth:`~horus_builtin.workflow.subworkflow.expander.SubworkflowExpander.
    _dump_body`, which this mirrors for :attr:`MapTask.task`.
    """
    for entry, artifact in zip(entries, artifacts, strict=False):
        if artifact.declared_path is not None:
            entry["path"] = str(artifact.declared_path)


class MapTask(HorusTask):
    """
    Wraps :attr:`task` and runs one clone of it per item of :attr:`over`,
    fanning every clone's outputs into this task's sole folder output.
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
    Inert placeholder: :meth:`_execute` is fully overridden and never
    delegates to ``self.executor``/``self.runtime``, so these exist only to
    satisfy ``BaseTask``'s required fields.
    """

    executor: BaseExecutor = Field(default_factory=ShellExecutor)

    task: BaseTask
    """
    The wrapped, per-clone task.
    """

    over: str
    """
    Id of one of *this* task's own :attr:`~BaseTask.inputs` carrying the
    collection to fan out over.
    """

    item_input: str
    """
    Id of the input on :attr:`task` that receives each item.
    """

    max_concurrency: int | None = None
    """Upper bound on clones dispatched at once; ``None`` means unbounded."""

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
        """
        if not any(a.id == self.over for a in self.inputs):
            raise MapConfigurationError(
                _("Map task '%(id)s' 'over' names unknown input '%(over)s'.")
                % {"id": self.id, "over": self.over}
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

        return self

    async def _execute(self) -> None:
        """
        Build one clone per item of :attr:`over` and run them all
        concurrently through :func:`~horus_builtin.workflow.scheduler.
        execute_task`, then aggregate their side artifacts onto this task.
        """
        wf = self.workflow
        if wf is None:
            raise MapConfigurationError(
                _("Map task '%(id)s' must run inside a workflow.")
                % {"id": self.id}
            )

        output = self.outputs[0]
        await self.target.mkdir(self.target.path_on_target(output))

        slots = await self._slots()
        clones = [self._clone(slot, item) for slot, item in slots]
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

    async def _slots(self) -> list[tuple[str, BaseArtifact]]:
        """
        Resolve the collection off :attr:`over` and return one
        ``(slot_name, item_artifact)`` pair per element, in a deterministic
        order.

        A :class:`FolderArtifact` source fans out over its children, sorted
        by name, each item pointed directly at the child's own path (zero
        copying); the slot is the child's name. Any other source's
        :meth:`~horus_runtime.core.artifact.base.BaseArtifact.read` must
        return a JSON list; each item is a fresh copy of the wrapped task's
        ``item_input`` artifact, written under this task's own working
        directory via the item's own
        :meth:`~horus_runtime.core.artifact.base.BaseArtifact.write`; the
        slot is a zero-padded index.
        """
        src = next(a for a in self.inputs if a.id == self.over)
        item_template = next(
            a for a in self.task.inputs if a.id == self.item_input
        )

        if isinstance(src, FolderArtifact):
            base = self.target.path_on_target(src)
            entries = await self.target.list_dir(base)
            return [
                (entry.name, self._item_at(item_template, Path(entry.path)))
                for entry in sorted(entries, key=lambda e: e.name)
            ]

        value = src.read()
        if not isinstance(value, list):
            raise MapConfigurationError(
                _(
                    "Map task '%(id)s' 'over' input '%(over)s' must be a "
                    "FolderArtifact or an artifact whose read() returns a "
                    "list."
                )
                % {"id": self.id, "over": self.over}
            )

        width = max(1, len(str(max(len(value) - 1, 0))))
        items_dir = Path(self.working_dir) / "items"
        template_declared = item_template.declared_path or item_template.path
        suffix = template_declared.suffix

        slots: list[tuple[str, BaseArtifact]] = []
        for i, element in enumerate(value):
            slot = f"{i:0{width}d}"
            item = item_template.model_copy(deep=True)
            item.path = items_dir / f"{slot}{suffix}"
            item.declared_path = item.path
            item.write(element)
            slots.append((slot, item))
        return slots

    @staticmethod
    def _item_at(template: BaseArtifact, path: Path) -> BaseArtifact:
        """A fresh copy of *template*, repointed at the absolute *path*."""
        item = template.model_copy(deep=True)
        item.path = path
        item.declared_path = path
        return item

    def _clone(self, slot: str, item: BaseArtifact) -> BaseTask:
        """
        Build one independent clone of :attr:`task` for *slot*, bound to
        *item*, with every other input shared verbatim from this task's own
        inputs and every relative output path re-rooted under this task's
        folder output.
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

        slot_root = Path(self.target.path_on_target(self.outputs[0])) / slot
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
        wrapped :attr:`task`'s own configuration, so editing the inner
        command invalidates this map's memoization too.

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
        config = json.dumps(
            {
                "runtime": self.runtime.model_dump(mode="json"),
                "executor": self.executor.model_dump(mode="json"),
                "task": task_doc,
            },
            sort_keys=True,
        )
        config_hash = hashlib.sha256(config.encode()).hexdigest()
        return TaskFingerprint(inputs=base.inputs, config_hash=config_hash)
