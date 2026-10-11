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
Tests for :mod:`horus_runtime.core.transfer.events`: the TransferEvents
``transfer_artifacts`` emits per staged input, and progress throttling.
"""

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.transfer.events import (
    TransferEvent,
    report_transfer_progress,
    transfer_scope,
)
from horus_runtime.core.transfer.strategy import BaseTransferStrategy
from horus_runtime.core.workflow.edge import WorkflowEdge


def _task(task_id: str, inputs: list[str], outputs: list[str]) -> HorusTask:
    return HorusTask(
        id=task_id,
        name=task_id,
        inputs=cast(
            list[BaseArtifact],
            [FileArtifact(id=i, path=Path(f"/tmp/{i}")) for i in inputs],
        ),
        outputs=cast(
            list[BaseArtifact],
            [FileArtifact(id=o, path=Path(f"/tmp/{o}")) for o in outputs],
        ),
        runtime=CommandRuntime(command="echo hi"),
        executor=ShellExecutor(),
        target=LocalTarget(),
    )


def _workflow() -> tuple[HorusWorkflow, HorusTask]:
    producer = _task("producer", [], ["out"])
    consumer = _task("consumer", ["inp"], [])
    wf = HorusWorkflow(
        name="wf",
        tasks=[producer, consumer],
        edges=[
            WorkflowEdge(
                source="producer",
                source_output="out",
                target="consumer",
                target_input="inp",
            )
        ],
    )
    return wf, consumer


def _template() -> TransferEvent:
    return TransferEvent(
        phase="started", task_id="t", input_id="i", source_artifact_id="a"
    )


@pytest.mark.unit
class TestTransferArtifactsEvents:
    """TransferEvents emitted per staged input by ``transfer_artifacts``."""

    async def test_same_location_transfer_emits_started_and_finished(
        self, horus_context: HorusContext
    ) -> None:
        """Even the same-filesystem shortcut is reported, keyed by edge."""
        wf, consumer = _workflow()
        events: list[TransferEvent] = []
        with patch.object(
            horus_context.bus, "emit", side_effect=events.append
        ):
            await wf.transfer_artifacts(consumer)

        assert [e.phase for e in events] == ["started", "finished"]
        # Each phase is its own event: the backend stores events under a
        # unique (run, event_id), so a shared id drops ``finished``.
        assert len({e.event_id for e in events}) == 2
        assert {
            (
                e.task_id,
                e.input_id,
                e.source_task_id,
                e.source_artifact_id,
                e.same_location,
            )
            for e in events
        } == {("consumer", "inp", "producer", "out", True)}

    async def test_failed_transfer_emits_failed_and_reraises(
        self, horus_context: HorusContext
    ) -> None:
        """A raising strategy reports ``failed`` and propagates."""
        wf, consumer = _workflow()

        class _Boom:
            async def transfer(self, *_: object) -> None:
                raise OSError("disk gone")

        events: list[TransferEvent] = []
        with (
            patch.object(horus_context.bus, "emit", side_effect=events.append),
            patch.object(
                BaseTransferStrategy, "get_from_registry", return_value=_Boom
            ),
            pytest.raises(OSError, match="disk gone"),
        ):
            await wf.transfer_artifacts(consumer)

        assert [e.phase for e in events] == ["started", "failed"]

    async def test_cancelled_transfer_emits_no_failed(
        self, horus_context: HorusContext
    ) -> None:
        """A cancel is not a failure: only ``started`` is reported."""
        wf, consumer = _workflow()

        class _Cancelled:
            async def transfer(self, *_: object) -> None:
                raise asyncio.CancelledError

        events: list[TransferEvent] = []
        with (
            patch.object(horus_context.bus, "emit", side_effect=events.append),
            patch.object(
                BaseTransferStrategy,
                "get_from_registry",
                return_value=_Cancelled,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await wf.transfer_artifacts(consumer)

        assert [e.phase for e in events] == ["started"]


@pytest.mark.unit
class TestReportTransferProgress:
    """Progress routing and throttling of ``report_transfer_progress``."""

    def test_noop_outside_scope(self, horus_context: HorusContext) -> None:
        """Strategies called outside a transfer scope report nothing."""
        with patch.object(horus_context.bus, "emit") as emit:
            report_transfer_progress(done=1, total=2)
        emit.assert_not_called()

    def test_throttles_bytes_but_not_stage_changes_or_completion(
        self, horus_context: HorusContext
    ) -> None:
        """Same-stage byte updates inside the interval are dropped."""
        events: list[TransferEvent] = []
        with (
            patch.object(horus_context.bus, "emit", side_effect=events.append),
            patch(
                "horus_runtime.core.transfer.events.time.monotonic",
                return_value=100.0,
            ),
            transfer_scope(_template()),
        ):
            report_transfer_progress(stage="transferring", done=1, total=10)
            report_transfer_progress(stage="transferring", done=2, total=10)
            report_transfer_progress(stage="unpacking")
            report_transfer_progress(stage="unpacking", done=10, total=10)

        progress = [e for e in events if e.phase == "progress"]
        assert [(e.stage, e.fraction) for e in progress] == [
            ("transferring", 0.1),
            ("unpacking", None),
            ("unpacking", 1.0),
        ]
        assert events[0].phase == "started"
        assert events[-1].phase == "finished"
