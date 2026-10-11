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
Artifact transfer events.

:class:`TransferEvent` reports the staging of one task input: emitted by
:meth:`BaseWorkflow.transfer_artifacts` around every transfer (``started``,
``finished``, ``failed``) and by strategies mid-transfer through
:func:`report_transfer_progress` (``progress``). The ids map one transfer onto
exactly one workflow edge, so a UI can animate the data moving along it.
"""

import datetime
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from horus_runtime.context import HorusContext
from horus_runtime.event.base import BaseEvent
from horus_runtime.logging import LoggerLevel

TransferPhase = Literal["started", "progress", "finished", "failed"]
TransferStage = Literal["packaging", "transferring", "uploading", "unpacking"]

# Progress events go over the bus (an HTTP POST per event in tc-os), so a
# byte-level callback is thinned to at most one event per interval unless the
# stage changes or the transfer completes.
PROGRESS_MIN_INTERVAL_SECONDS = 0.25


class TransferEvent(BaseEvent):
    """
    One input of *task_id* being staged from its source onto the task target.

    ``source_task_id`` is the producing task, or ``None`` for a root artifact
    (whose id is then ``source_artifact_id``). ``fraction`` is set only by
    strategies that know the byte count; otherwise progress is a ``stage``.
    ``cache_restore`` marks an artifact-cache restore of one of *task_id*'s
    own outputs (``input_id`` is then the output id) rather than an input
    staged along an edge.
    """

    event_type: str = "artifact_transfer"
    # Several per transfer and message-less: keep them out of INFO logs.
    level: LoggerLevel = "DEBUG"
    phase: TransferPhase
    task_id: str
    input_id: str
    source_task_id: str | None = None
    source_artifact_id: str
    strategy: str | None = None
    same_location: bool = False
    is_value: bool = False
    cache_restore: bool = False
    stage: TransferStage | None = None
    fraction: float | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None


@dataclass
class _TransferScope:
    """The transfer in progress in this context, plus throttle state."""

    template: TransferEvent
    last_emit: float = 0.0
    last_stage: str | None = None


_current_transfer: ContextVar[_TransferScope | None] = ContextVar(
    "horus_current_transfer", default=None
)


def emit_transfer_event(event: TransferEvent) -> None:
    """Emit *event* on the bus; a no-op outside a booted HorusContext."""
    try:
        ctx = HorusContext.get_context()
    except RuntimeError:
        return
    ctx.bus.emit(event)


def _derive(
    template: TransferEvent, update: dict[str, object]
) -> TransferEvent:
    """
    A new event for *template*'s transfer with *update* applied. Each one is a
    distinct event: a fresh ``event_id`` (persisted per run under a unique
    constraint, so a copied id would drop every event after ``started``) and
    timestamp.
    """
    return template.model_copy(
        update={
            **update,
            "event_id": uuid.uuid4(),
            "timestamp": datetime.datetime.now(datetime.UTC),
        }
    )


@contextmanager
def transfer_scope(template: TransferEvent) -> Iterator[None]:
    """
    Emit ``started`` for *template*, route :func:`report_transfer_progress`
    calls inside the block to it, then emit ``finished`` (or ``failed`` when
    the block raises, re-raising). A cancellation is not a failure: it emits
    nothing more, and a UI drops the open transfer when the run ends.
    """
    emit_transfer_event(_derive(template, {"phase": "started"}))
    token = _current_transfer.set(_TransferScope(template))
    try:
        yield
    except Exception:
        emit_transfer_event(_derive(template, {"phase": "failed"}))
        raise
    finally:
        _current_transfer.reset(token)
    emit_transfer_event(_derive(template, {"phase": "finished"}))


def report_transfer_progress(
    *,
    stage: TransferStage | None = None,
    done: int | None = None,
    total: int | None = None,
) -> None:
    """
    Report progress of the transfer running in this context.

    Strategies call this without knowing which task or edge they serve; it is
    a no-op when no :func:`transfer_scope` is active (e.g. a direct
    ``strategy.transfer`` call). ``done``/``total`` are bytes; ``fraction`` is
    only derived when ``total`` is known and positive.
    """
    scope = _current_transfer.get()
    if scope is None:
        return
    fraction = (
        min(done / total, 1.0)
        if done is not None and total is not None and total > 0
        else None
    )
    now = time.monotonic()
    if (
        stage == scope.last_stage
        and fraction != 1.0
        and now - scope.last_emit < PROGRESS_MIN_INTERVAL_SECONDS
    ):
        return
    scope.last_emit = now
    scope.last_stage = stage
    emit_transfer_event(
        _derive(
            scope.template,
            {
                "phase": "progress",
                "stage": stage,
                "fraction": fraction,
                "bytes_done": done,
                "bytes_total": total,
            },
        )
    )
