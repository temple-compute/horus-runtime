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
Base Task. The task represents the unit of work that is executed by the Horus
runtime. The base task provides the foundational functionality for defining and
executing tasks, and should be ingested by the executor.
"""

from abc import abstractmethod
from asyncio import CancelledError
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, final
from uuid import uuid4

from pydantic import (
    Field,
    PrivateAttr,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_serializer,
    field_validator,
    model_validator,
)

from horus_builtin.event.task_event import HorusTaskEvent
from horus_runtime.context import HorusContext, running_task, task_env
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.executor.exceptions import IncompatibleRuntimeError
from horus_runtime.core.interaction.transport import BaseInteractionTransport
from horus_runtime.core.resources import ResourceRequest
from horus_runtime.core.runtime.base import BaseRuntime
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.target.ref import target_or_ref_schema
from horus_runtime.core.task.exceptions import (
    TargetLostError,
    summarize_exception,
)
from horus_runtime.core.task.status import SkipReason, TaskStatus
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.middleware.task import TaskMiddleware, TaskMiddlewareContext
from horus_runtime.registry.auto_registry import AutoRegistry
from horus_runtime.secrets import TaskSecret, env_name_error

if TYPE_CHECKING:
    from horus_runtime.core.workflow.base import BaseWorkflow

InputDigests = Callable[[], Awaitable[dict[str, str | None]]]
"""Returns each input's digest by id, ``None`` where it cannot be hashed."""

MAX_TARGET_LOSS_RETRIES = 1
"""How many times a task is re-run after a :class:`TargetLostError`."""


class BaseTask(AutoRegistry, entry_point="task"):
    """
    The base task. This class provides the foundational functionality for
    defining and executing tasks, and should be ingested by the executor.
    """

    registry_key: ClassVar[str] = "kind"
    """
    The 'registry_key' field is used to identify the specific type of task.
    """

    kind: str
    """
    The 'kind' field is used to identify the specific type of task.
    """

    kind_name: ClassVar[str] = "BaseTask"
    """
    Human-readable name for this task type, used in the UI.
    """

    kind_description: ClassVar[str] = _("Horus base task")
    """
    Description of this task type, used in the UI.
    """

    dispatches_children: ClassVar[bool] = False
    """
    Whether running this task means dispatching child tasks and waiting on
    them (a map). Such a task holds no machine slot or capacity of its own,
    or its children could never get one.
    """

    id: str = Field(pattern=r"^([^@]|$)")
    """
    The task ID. Unique within a workflow: it is the DAG node key (edges,
    ``working_dir``, and UI positions all reference it). May not start with
    ``@``, which marks synthetic (per-machine) ledger rows.
    """

    definition_id: str | None = None
    """
    Optional reference to the catalog/library task this placement was created
    from. Not unique: the same reusable task placed twice shares one
    ``definition_id`` while each placement keeps its own unique ``id``.
    ``None`` for ad-hoc tasks not sourced from the catalog.
    """

    name: str
    """
    Human-readable name for this task.
    """

    description: str = ""
    """
    Optional free-text description of the task, shown in the UI.
    """

    inputs: list[BaseArtifact] = Field(default_factory=list)
    """
    Input artifacts for this task. These are the artifacts that the task
    depends on.
    """

    unfingerprinted_inputs: list[str] = Field(default_factory=list)
    """
    Ids of inputs left out of the task's fingerprint, so a change to them
    does not invalidate its recorded outputs. A map sets it on its clones
    for the collection being mapped, which each clone's item already covers.
    Stored with the task so a clone reloaded from a snapshot still matches
    the manifest it wrote.
    """

    outputs: list[BaseArtifact] = Field(default_factory=list)
    """
    Output artifacts for this task. These are the artifacts that the task
    produces.
    """

    side_artifacts: list[BaseArtifact] = Field(
        default_factory=list, exclude=True
    )
    """
    Transient, undeclared artifacts produced by the task that the user may want
    to inspect (logs, plots, small intermediate files, etc.) but which are not
    consumed by any downstream task.
    """

    executor: BaseExecutor
    """
    The executor that should execute this task. The executor is responsible for
    running the task in the appropriate environment (e.g., locally, on a remote
    server, in a container, etc.).
    """

    runtime: BaseRuntime
    """
    The runtime that should be used to execute this task. The runtime defines
    the actual command, program or script to run.
    """

    target: BaseTarget = Field(json_schema_extra=target_or_ref_schema)
    """
    The target that indicates where this task should be dispatched, or, in
    a document, ``{ref: <name>}`` naming one of the workflow's ``targets:``.
    """

    @field_serializer("target", mode="wrap")
    def _dump_target(
        self,
        target: BaseTarget,
        handler: SerializerFunctionWrapHandler,
        info: SerializationInfo,
    ) -> object:
        """
        Dump a named (shared) target as ``{ref: <name>}`` so a dumped
        workflow keeps sharing it; ``context={"inline_targets": True}``
        dumps it in full instead.
        """
        name = target._ref  # noqa: SLF001
        if name is not None and not (info.context or {}).get("inline_targets"):
            return {"ref": name}
        return handler(target)

    secrets: dict[str, TaskSecret] = Field(default_factory=dict)
    """
    Environment variables holding credentials, by name, e.g.
    ``{HF_TOKEN: {value: "${secret:hf_token}", name: "Hugging Face"}}`` or
    the shorthand ``{HF_TOKEN: "${secret:hf_token}"}``. Each is resolved when
    the task starts (see :meth:`Secret.resolve`) and set on every command the
    task runs on its target; a missing one fails the task before it runs.
    """

    @field_validator("secrets")
    @classmethod
    def _check_secret_names(
        cls, secrets: dict[str, TaskSecret]
    ) -> dict[str, TaskSecret]:
        """Each key must be a usable, non-reserved env var name."""
        for name in secrets:
            error = env_name_error(name)
            if error is not None:
                raise ValueError(
                    _("Secret %(name)s: %(error)s")
                    % {"name": repr(name), "error": error}
                )
        return secrets

    resources: ResourceRequest | None = None
    """
    Advisory compute requirements for this task (CPUs, GPUs, memory, …). Read
    by resource-aware targets (e.g. Slurm, Terraform) when provisioning;
    targets that do not understand resources ignore it. Defaults to ``None`` so
    existing workflows and YAML stay valid.
    """

    status: TaskStatus = TaskStatus.IDLE
    """
    The current status of the task's execution.
    """

    skip_reason: SkipReason | None = None
    """
    Why the task was skipped, set only alongside ``TaskStatus.SKIPPED``. Lets a
    consumer tell a memoized cache hit apart from a branch that was not taken
    or a task blocked by an upstream failure, which share the status but mean
    different things to a reader.
    """

    error: str | None = None
    """
    One-line summary of why the task failed (``"<Type>: <message>"``), set
    only alongside ``TaskStatus.FAILED``. The full traceback is in the task's
    log.
    """

    started_at: datetime | None = None
    """
    When this task's status last moved to ``RUNNING`` (UTC). ``None`` before
    the task has ever run.
    """

    finished_at: datetime | None = None
    """
    When this task's status last reached a terminal state (UTC). ``None``
    while idle, pending, or running.
    """

    runs: int = 0
    """
    Number of times this task has been run. This can be used for tracking and
    debugging purposes.
    """

    skip_if_complete: bool = True
    """
    Whether to skip execution of this task if it is already complete.
    """

    interaction: BaseInteractionTransport | None = None
    """
    The interaction transport currently associated with the task run.
    """

    _execution_id: str | None = PrivateAttr(default=None)
    """
    Identifier for this task invocation's isolated working directory.

    This stays runtime-only: a workflow definition names the task's base
    directory, while every execution receives a fresh child directory below
    it.  Keeping it private also prevents a historical run id from being
    written back into workflow YAML or a hosted workflow snapshot.
    """

    _run_scope: str | None = PrivateAttr(default=None)
    """
    Opaque relative path fragment nested under the target's own working
    directory, set by :meth:`BaseWorkflow._anchor_task` for tasks whose
    target is not co-located with the orchestrator. Private for the same
    reason as :attr:`_execution_id`: it must be recomputed fresh from the
    current run every time, never round-tripped through a stored snapshot.
    """

    _target_loss_retries: int = PrivateAttr(default=0)
    """Re-runs spent on :class:`TargetLostError`, up to the max."""

    _staged_digests: dict[str, str] | None = PrivateAttr(default=None)
    """
    Set by the scheduler for one run: the digests of the non-pinned inputs
    it just staged, taken at their source, so the run need not hash the
    staged copies again.
    """

    _retry_on_target_loss: bool = PrivateAttr(default=False)
    """
    Set by the scheduler before dispatch: a :class:`TargetLostError` from
    this run will be retried, so :meth:`run` leaves the task ``PENDING``
    instead of ``FAILED``.
    """

    @property
    def fingerprinted_inputs(self) -> list[BaseArtifact]:
        """
        The inputs whose content decides whether recorded outputs are stale.
        """
        return [
            a for a in self.inputs if a.id not in self.unfingerprinted_inputs
        ]

    @property
    def working_dir(self) -> str:
        """
        The task's working directory: a per-task, per-invocation folder under
        its target's working directory. Inputs are materialized here and
        outputs and side-products are written relative to it.

        A non-co-located target's own directory is further scoped by
        :attr:`_run_scope` (workflow/run isolation) before the task id.
        Before the task is invoked the legacy ``<target>/<task-id>`` path is
        returned, which keeps setup and inspection code deterministic. Once
        execution begins, the invocation id is appended so re-running a task
        never reuses a previous job's directory.
        """
        path = Path(self.target.resolved_working_directory)
        if self._run_scope:
            path /= self._run_scope
        path /= self.id
        if self._execution_id is not None:
            path /= self._execution_id
        return path.as_posix()

    @property
    def workflow(self) -> "BaseWorkflow | None":
        """
        The live workflow this task belongs to, when running inside one.

        Convenience accessor for the current :class:`HorusContext`'s
        workflow (set by :meth:`BaseWorkflow.run` for the duration of a run),
        so a running task's body can reach the DAG-mutation API without
        importing ``HorusContext`` itself, e.g.::

            def my_step(task: BaseTask) -> None:
                assert task.workflow is not None
                task.workflow.add_task(...)
                task.workflow.add_edge(...)

        ``None`` outside of a workflow run.
        """
        return HorusContext.get_context().workflow

    @property
    def side_artifacts_dir(self) -> str:
        """
        Directory **on the target host** where side-product artifacts are
        written by convention. This is a target-side path: it must not be
        opened with local filesystem operations for remote targets. Use the
        target channel (``list_dir`` / ``get_file``) to read it.
        """
        return (Path(self.working_dir) / "side-artifacts").as_posix()

    @model_validator(mode="after")
    def _validate_runtime_compatibility(self) -> Self:
        if not isinstance(self.runtime, self.executor.runtimes):
            raise IncompatibleRuntimeError(
                _(
                    "Runtime '%(runtime)s' is not compatible with executor "
                    "'%(executor)s'. Expected one of: %(expected)s"
                )
                % {
                    "runtime": type(self.runtime).__name__,
                    "executor": type(self.executor).__name__,
                    "expected": [r.__name__ for r in self.executor.runtimes],
                }
            )
        return self

    @final
    async def run(self) -> None:
        """
        Execute the task, managing status transitions automatically.

        Subclasses must implement ``_run()`` instead of overriding this method.
        Status is driven entirely here:

        - ``SKIPPED``     — set when ``skip_if_complete`` is true and outputs
                            exist (returns early)
        - ``CONFIGURING`` — set on entry when not skipped; the executor flips
                            this to ``RUNNING`` once its own work starts (see
                            ``BaseExecutor.execute``)
        - ``COMPLETED``   — set on clean exit
        - ``CANCELED``  — set when ``CancelledError`` is raised
        - ``FAILED``    — set on any other exception (re-raised after)
        - ``PENDING``   — instead of ``FAILED`` for a
                          :class:`TargetLostError` the scheduler will retry

        Terminal statuses are set as soon as ``_run()`` returns or raises,
        still inside the ``TaskMiddleware`` chain, so middleware sees them on
        its way out; a middleware that later raises turns them into ``FAILED``
        or ``CANCELED``.
        """
        # Publish this task as the "current task" for the duration of its
        # execution so code it invokes (notably ``BaseWorkflow.add_task``) can
        # discover its creator and gate any runtime-added task behind it.
        with running_task(self.id):
            # Allocate this before the completion check too. A task that is
            # forced to run, or whose cached outputs are invalidated, must not
            # accidentally reuse the prior invocation's cwd.
            self._execution_id = uuid4().hex
            if self.skip_if_complete and await self.is_complete():
                self.mark_skipped_complete()
                return

            self.status = TaskStatus.CONFIGURING
            self.error = None
            self.skip_reason = None
            self.started_at = datetime.now(UTC)
            self.finished_at = None
            horus_logger.log.debug(
                _("Task %(task_name)s status → CONFIGURING")
                % {"task_name": self.name}
            )

            try:

                async def mkdir_and_run() -> None:
                    # Every executor receives a real, empty directory for this
                    # invocation. This is intentionally here rather than in a
                    # specific executor so shell, Python, remote, and plugin
                    # executors all share the same isolation guarantee.
                    #
                    # It runs *inside* the middleware chain so middleware that
                    # retargets the task (e.g. an orchestrator re-anchoring a
                    # runtime-added task's working directory) has already run:
                    # creating the directory first would create it at the old
                    # path and leave the new one missing.
                    await self.target.mkdir(self.working_dir)
                    # The terminal status is also set here, inside the chain,
                    # so every middleware observes it on its way out (e.g. one
                    # reporting status in its ``finally``), and a failure is
                    # logged here, while this task is still current, so a
                    # middleware uploading the task's log in its own cleanup
                    # (whatever its position in the chain) uploads the error
                    # too. The outer handlers below still cover failures
                    # raised by middleware itself.
                    try:
                        env = {
                            k: v.value.resolve()
                            for k, v in self.secrets.items()
                        }
                        with task_env(env):
                            await self._run()
                    except CancelledError:
                        self._finish(TaskStatus.CANCELED)
                        raise
                    except Exception as exc:
                        self._fail_or_wait_retry(exc)
                        raise
                    self._finish(TaskStatus.COMPLETED)

                await TaskMiddleware.call_with_middleware(
                    TaskMiddlewareContext(task=self),
                    mkdir_and_run,
                )
            except CancelledError:
                self._finish(TaskStatus.CANCELED)
                raise
            except Exception as exc:
                self._fail_or_wait_retry(exc)
                raise
            else:
                self._finish(TaskStatus.COMPLETED)

    def _fail_or_wait_retry(self, exc: Exception) -> None:
        """
        Record ``FAILED``, or ``PENDING`` when the scheduler will retry
        *exc* on a new machine, so no status push shows a failure first.
        """
        if isinstance(exc, TargetLostError) and self._retry_on_target_loss:
            self.status = TaskStatus.PENDING
            self.error = None
        else:
            self._finish(TaskStatus.FAILED, exc)

    def _finish(
        self, status: TaskStatus, exc: Exception | None = None
    ) -> None:
        """
        Record terminal *status* and ``finished_at``; a no-op if the task is
        already in that status, so the outer handlers in :meth:`run` keep the
        timestamp (and error) set inside the middleware chain.

        For ``FAILED``, *exc* is logged with its traceback and summarized
        into ``error``.
        """
        if self.status is status:
            return
        self.status = status
        self.finished_at = datetime.now(UTC)
        if exc is not None:
            self.error = summarize_exception(exc)
            # The attached exception carries the reason (and traceback).
            horus_logger.log.opt(exception=exc).error(
                _("Task %(task_name)s failed") % {"task_name": self.name}
            )
        messages = {
            TaskStatus.COMPLETED: _("Task %(task_name)s status → COMPLETED"),
            TaskStatus.FAILED: _("Task %(task_name)s status → FAILED"),
            TaskStatus.CANCELED: _("Task %(task_name)s status → CANCELED"),
        }
        horus_logger.log.debug(messages[status] % {"task_name": self.name})

    async def sync_status(self) -> TaskStatus:
        """
        Refresh ``self.status`` from the target and return the updated value.

        For local in-process targets this is a no-op read. For remote targets
        (SSH, agent) this performs whatever async probe the target requires
        (HTTP poll, SSH check, etc.) and caches the result in ``self.status``
        so that synchronous callers can read it without I/O.
        """
        self.status = await self.target.get_status()
        return self.status

    @abstractmethod
    async def _run(self) -> None:
        """
        Task-specific execution logic. Implement this in subclasses.
        Do not set ``self.status`` here; ``run()`` manages it.
        """

    @abstractmethod
    async def is_complete(self) -> bool:
        """
        Determine whether the task is complete by checking the existence and
        integrity of its output artifacts. This method should be implemented by
        subclasses to define the specific logic for determining task completion
        based on its outputs.
        """

    async def is_complete_before_transfer(
        self, input_digests: InputDigests
    ) -> bool:
        """
        Like :meth:`is_complete`, but decided before any input is staged on
        the task's target. *input_digests* returns each input's digest at its
        source (see ``BaseWorkflow.source_digests``); it is only awaited
        when the outcome depends on it.

        The default cannot decide without staged inputs and returns
        ``False``: the task is transferred and ``run()`` checks as usual.
        """
        del input_digests
        return False

    async def manifest_matches(
        self, manifest: bytes, input_digests: InputDigests
    ) -> bool:
        """
        Whether *manifest*, a completion manifest in this task's own format
        (e.g. a cached copy), matches the task's current inputs and
        configuration. Reads nothing on the target, so a plugin can ask it
        before restoring outputs there.

        The default has no manifest and returns ``False``.
        """
        del manifest, input_digests
        return False

    def mark_skipped_complete(self) -> None:
        """
        Record that the task was skipped because it is already complete.
        """
        HorusContext.get_context().bus.emit(
            HorusTaskEvent(
                message=_("Skipping task %(task_name)s. Already complete.")
                % {"task_name": self.name},
                task_id=self.id,
                task_name=self.name,
            )
        )
        self.status = TaskStatus.SKIPPED
        self.skip_reason = SkipReason.COMPLETE
        horus_logger.log.debug(
            _("Task %(task_name)s status → SKIPPED") % {"task_name": self.name}
        )

    @final
    async def reset(self) -> None:
        """
        Reset the task. This allows the task to be re-run from scratch.

        Resets ``self.status`` to ``IDLE`` and delegates subclass-specific
        reset logic to ``_reset()``.
        """
        self.status = TaskStatus.IDLE
        self.skip_reason = None
        self.error = None
        self.started_at = None
        self.finished_at = None
        self._target_loss_retries = 0
        self._retry_on_target_loss = False
        horus_logger.log.debug(
            _("Task %(task_name)s reset → IDLE") % {"task_name": self.name}
        )
        await self._reset()

    @abstractmethod
    async def _reset(self) -> None:
        """
        Subclass-specific reset logic. Override this in subclasses when
        additional state must be cleared on reset. Do not set ``self.status``
        here; ``reset()`` manages it.
        """
