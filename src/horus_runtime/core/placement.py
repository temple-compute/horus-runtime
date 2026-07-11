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
Optional, opt-in resource/target-aware placement gating for the scheduler.

A workflow may declare how much compute is physically available at each
``location_id`` (see :attr:`BaseTarget.location_id`). When it does,
:class:`PlacementManager` gates dispatch of any *ready* task that also
declares :attr:`~horus_runtime.core.task.base.BaseTask.resources`, so a wide
fan-out cannot oversubscribe a shared machine (e.g. only 2 GPUs available means
at most 2 GPU-requesting tasks run at once there).

Nothing here changes behavior for workflows that don't declare capacity:
:meth:`PlacementManager.acquire` is a no-op whenever the task has no
``resources``, or its target's location has no declared capacity, leaving
concurrency governed exactly as before by :class:`TargetPool` and
``workflow.max_concurrency``.
"""

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from horus_runtime.core.resources import ResourceRequest
from horus_runtime.i18n import tr as _

#: The resource dimensions placement gates on. ``walltime`` is a scheduling
#: hint (a deadline), not a consumable quantity, so it is never gated here.
_DIMENSIONS = ("cpus", "gpus", "memory_gb", "vram_gb")


class ResourceCapacity(BaseModel):
    """
    Total compute capacity available at one ``location_id``.

    Every field defaults to ``None``, meaning "unconstrained": a task
    requesting that dimension is never gated on it, regardless of how much it
    asks for. Only dimensions the location operator explicitly sets are
    enforced, so declaring ``ResourceCapacity(gpus=2)`` caps concurrent GPU
    usage at that location while leaving CPU/memory ungated.
    """

    model_config = ConfigDict(extra="forbid")

    cpus: int | None = Field(default=None, ge=0)
    gpus: int | None = Field(default=None, ge=0)
    memory_gb: int | None = Field(default=None, ge=0)
    vram_gb: int | None = Field(default=None, ge=0)


class InsufficientCapacityError(Exception):
    """
    Raised when a task requests more of some resource dimension than a
    location's *total* declared capacity, at the moment it would first be
    dispatched there.

    Checked against the location's total (fixed) capacity rather than its
    currently free capacity, so this is raised immediately instead of the
    task waiting forever for capacity that can never materialize.
    """

    def __init__(
        self,
        task_name: str,
        location_id: str,
        dimension: str,
        requested: int,
        total: int,
    ) -> None:
        self.task_name = task_name
        self.location_id = location_id
        self.dimension = dimension
        self.requested = requested
        self.total = total
        super().__init__(
            _(
                "Task '%(task_name)s' requests %(requested)s %(dimension)s "
                "at location '%(location_id)s', which only ever has "
                "%(total)s available. This request can never be satisfied."
            )
            % {
                "task_name": task_name,
                "requested": requested,
                "dimension": dimension,
                "location_id": location_id,
                "total": total,
            }
        )


def _requested_amounts(resources: ResourceRequest) -> dict[str, int]:
    """
    The non-trivial dimensions *resources* asks for.

    ``gpus`` defaults to ``0`` (rather than ``None``) on
    :class:`ResourceRequest`, so a request for zero GPUs is treated the same
    as not requesting GPUs at all: it is omitted, not gated as "at most 0".
    """
    amounts: dict[str, int] = {}
    if resources.cpus is not None:
        amounts["cpus"] = resources.cpus
    if resources.gpus:
        amounts["gpus"] = resources.gpus
    if resources.memory_gb is not None:
        amounts["memory_gb"] = resources.memory_gb
    if resources.vram_gb is not None:
        amounts["vram_gb"] = resources.vram_gb
    return amounts


class PlacementManager:
    """
    Gates ready-task dispatch against declared, finite per-location capacity.

    Construct one per workflow run from an optional
    ``{location_id: ResourceCapacity}`` map (``workflow.capacity``). With no
    map, or an empty one, :meth:`acquire` always returns immediately:
    placement is purely additive and opt-in.

    Not thread-safe; intended for single-event-loop use exactly like
    :class:`~horus_builtin.workflow.scheduler.TargetPool`, which it is meant
    to be used alongside (placement decides *whether* a task may take a
    slot; the pool still hands out the concrete target object).
    """

    def __init__(self, capacity: "dict[str, ResourceCapacity] | None") -> None:
        self._total: dict[str, ResourceCapacity] = dict(capacity or {})
        self._remaining: dict[str, dict[str, int]] = {
            location_id: {
                dim: getattr(cap, dim)
                for dim in _DIMENSIONS
                if getattr(cap, dim) is not None
            }
            for location_id, cap in self._total.items()
        }
        self._condition = asyncio.Condition()

    def _fits(self, location_id: str, requested: dict[str, int]) -> bool:
        remaining = self._remaining[location_id]
        return all(
            remaining[dim] >= amount
            for dim, amount in requested.items()
            if dim in remaining
        )

    async def acquire(
        self,
        task_name: str,
        location_id: str,
        resources: ResourceRequest | None,
    ) -> None:
        """
        Wait until *location_id* has room for *resources*, then reserve it.

        A no-op whenever *resources* is ``None`` or *location_id* has no
        declared capacity: such tasks are never gated, so behavior stays
        identical to a workflow with no placement configured at all.

        Raises:
            InsufficientCapacityError: If some requested dimension exceeds
                the location's total declared capacity, so the request could
                never be satisfied even with nothing else running there.
        """
        if resources is None:
            return
        total = self._total.get(location_id)
        if total is None:
            return
        requested = _requested_amounts(resources)
        if not requested:
            return

        # Checked against the fixed total (not the current remaining) before
        # ever waiting, so an unsatisfiable request fails fast instead of
        # blocking the ready-set loop forever.
        for dim, amount in requested.items():
            dim_total = getattr(total, dim)
            if dim_total is not None and amount > dim_total:
                raise InsufficientCapacityError(
                    task_name, location_id, dim, amount, dim_total
                )

        async with self._condition:
            await self._condition.wait_for(
                lambda: self._fits(location_id, requested)
            )
            remaining = self._remaining[location_id]
            for dim, amount in requested.items():
                if dim in remaining:
                    remaining[dim] -= amount

    async def release(
        self,
        location_id: str,
        resources: ResourceRequest | None,
    ) -> None:
        """Return capacity reserved by a prior, matching :meth:`acquire`."""
        if resources is None:
            return
        if location_id not in self._total:
            return
        requested = _requested_amounts(resources)
        if not requested:
            return

        async with self._condition:
            remaining = self._remaining[location_id]
            for dim, amount in requested.items():
                if dim in remaining:
                    remaining[dim] += amount
            self._condition.notify_all()
