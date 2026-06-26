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
Unit tests for the portable ResourceRequest model and its use on BaseTask.
"""

import pytest
from pydantic import ValidationError

from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.resources import ResourceRequest


def _make_task(resources: ResourceRequest | None = None) -> HorusTask:
    """Build a minimal concrete ``HorusTask`` for round-trip checks."""
    return HorusTask(
        id="task_id",
        name="task",
        runtime=CommandRuntime(command="echo hi"),
        executor=ShellExecutor(),
        target=LocalTarget(),
        resources=resources,
    )


@pytest.mark.unit
class TestResourceRequest:
    """Tests for the ResourceRequest pydantic model."""

    def test_defaults(self) -> None:
        """All fields default to None except gpus, which defaults to 0."""
        req = ResourceRequest()
        assert req.cpus is None
        assert req.gpus == 0
        assert req.memory_gb is None
        assert req.vram_gb is None
        assert req.walltime is None

    def test_explicit_values_are_preserved(self) -> None:
        """Provided values are stored verbatim."""
        req = ResourceRequest(
            cpus=8,
            gpus=2,
            memory_gb=64,
            vram_gb=24,
            walltime="01:30:00",
        )
        assert req.cpus == 8
        assert req.gpus == 2
        assert req.memory_gb == 64
        assert req.vram_gb == 24
        assert req.walltime == "01:30:00"

    def test_extra_fields_are_forbidden(self) -> None:
        """Unknown fields raise rather than being silently dropped."""
        with pytest.raises(ValidationError):
            ResourceRequest(bogus=1)  # type: ignore[call-arg]

    def test_json_round_trip(self) -> None:
        """A ResourceRequest survives model_dump(json) -> model_validate."""
        req = ResourceRequest(cpus=4, gpus=1, memory_gb=16)
        restored = ResourceRequest.model_validate(req.model_dump(mode="json"))
        assert restored == req


@pytest.mark.unit
class TestTaskResources:
    """Tests for the advisory ``resources`` field on BaseTask."""

    def test_resources_default_is_none(self) -> None:
        """Existing tasks/YAML stay valid: resources defaults to None."""
        assert _make_task().resources is None

    def test_round_trip_without_resources(self) -> None:
        """A task with no resources round-trips with resources still None."""
        task = _make_task()
        restored = HorusTask.model_validate(task.model_dump(mode="json"))
        assert restored.resources is None

    def test_round_trip_with_resources(self) -> None:
        """A task's resources survive model_dump(json) -> model_validate."""
        resources = ResourceRequest(cpus=4, gpus=1, memory_gb=32)
        task = _make_task(resources=resources)
        restored = HorusTask.model_validate(task.model_dump(mode="json"))
        assert restored.resources == resources
