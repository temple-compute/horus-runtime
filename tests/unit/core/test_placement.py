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
Unit tests for :class:`PlacementManager` in isolation from the scheduler.
"""

import asyncio

import pytest

from horus_runtime.core.placement import (
    InsufficientCapacityError,
    PlacementManager,
    ResourceCapacity,
)
from horus_runtime.core.resources import ResourceRequest


@pytest.mark.unit
class TestResourceCapacity:
    """Tests for the ``ResourceCapacity`` pydantic model."""

    def test_defaults_are_unconstrained(self) -> None:
        cap = ResourceCapacity()
        assert cap.cpus is None
        assert cap.gpus is None
        assert cap.memory_gb is None
        assert cap.vram_gb is None

    def test_extra_fields_are_forbidden(self) -> None:
        with pytest.raises(Exception, match="bogus"):
            ResourceCapacity(bogus=1)  # type: ignore[call-arg]


@pytest.mark.unit
class TestPlacementManagerNoOp:
    """
    Whenever nothing declares capacity/resources, ``acquire``/``release``
    are no-ops: placement is purely additive and opt-in.
    """

    async def test_no_capacity_map_never_gates(self) -> None:
        manager = PlacementManager(None)
        await asyncio.wait_for(
            manager.acquire(
                "t", "loc", ResourceRequest(gpus=1000)
            ),
            timeout=1,
        )
        # No error, no hang: an undeclared location is unconstrained.

    async def test_task_with_no_resources_never_gates(self) -> None:
        manager = PlacementManager({"loc": ResourceCapacity(gpus=1)})
        # Two immediate acquisitions with resources=None never block each
        # other, even though the location only declares one GPU.
        await asyncio.wait_for(
            asyncio.gather(
                manager.acquire("a", "loc", None),
                manager.acquire("b", "loc", None),
            ),
            timeout=1,
        )

    async def test_location_absent_from_capacity_map_is_unconstrained(
        self,
    ) -> None:
        manager = PlacementManager({"declared-loc": ResourceCapacity(gpus=1)})
        await asyncio.wait_for(
            manager.acquire(
                "t", "other-loc", ResourceRequest(gpus=1000)
            ),
            timeout=1,
        )

    async def test_zero_gpu_request_is_not_gated(self) -> None:
        """
        ``ResourceRequest.gpus`` defaults to 0, meaning "no GPU requested",
        not "at most 0 GPUs": it must not be treated as a real request.
        """
        manager = PlacementManager({"loc": ResourceCapacity(gpus=0)})
        await asyncio.wait_for(
            manager.acquire("t", "loc", ResourceRequest()), timeout=1
        )


@pytest.mark.unit
class TestPlacementManagerGating:
    """Tests for real capacity gating and release."""

    async def test_acquire_release_round_trip_restores_capacity(
        self,
    ) -> None:
        manager = PlacementManager({"loc": ResourceCapacity(gpus=1)})
        resources = ResourceRequest(gpus=1)

        await manager.acquire("a", "loc", resources)
        # A second, concurrent acquire would now block: prove it does by
        # racing it against a release and checking it only unblocks after.
        second = asyncio.ensure_future(
            manager.acquire("b", "loc", resources)
        )
        await asyncio.sleep(0.01)
        assert not second.done()

        await manager.release("loc", resources)
        await asyncio.wait_for(second, timeout=1)

    async def test_requesting_more_than_total_raises_immediately(
        self,
    ) -> None:
        manager = PlacementManager({"loc": ResourceCapacity(gpus=2)})
        with pytest.raises(InsufficientCapacityError) as exc_info:
            await asyncio.wait_for(
                manager.acquire(
                    "greedy", "loc", ResourceRequest(gpus=5)
                ),
                timeout=1,
            )
        assert exc_info.value.dimension == "gpus"
        assert exc_info.value.requested == 5
        assert exc_info.value.total == 2

    async def test_unconstrained_dimension_on_declared_location_passes(
        self,
    ) -> None:
        """
        A location that only declares ``gpus`` leaves ``cpus`` ungated, even
        though the location itself has a capacity entry.
        """
        manager = PlacementManager({"loc": ResourceCapacity(gpus=2)})
        await asyncio.wait_for(
            manager.acquire(
                "t", "loc", ResourceRequest(cpus=999, gpus=1)
            ),
            timeout=1,
        )

    async def test_two_gpu_capacity_admits_two_but_not_three_concurrently(
        self,
    ) -> None:
        manager = PlacementManager({"loc": ResourceCapacity(gpus=2)})
        resources = ResourceRequest(gpus=1)
        current = 0
        max_seen = 0

        async def run_one(name: str) -> None:
            nonlocal current, max_seen
            await manager.acquire(name, "loc", resources)
            current += 1
            max_seen = max(max_seen, current)
            await asyncio.sleep(0.02)
            current -= 1
            await manager.release("loc", resources)

        await asyncio.wait_for(
            asyncio.gather(*(run_one(str(i)) for i in range(4))),
            timeout=5,
        )
        assert max_seen == 2
