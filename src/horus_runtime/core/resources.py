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
Portable, target-agnostic compute resources for tasks.

Two halves that mirror each other:

- :class:`ResourceRequest` — what a task *asks for*. Advisory: resource-aware
  targets (Slurm, Terraform, etc.) translate the hints into their own
  provisioning primitives, and targets that ignore them are unaffected.
- :class:`ResourceScope` — where a task's work *actually runs*, so an observer
  can find it and measure what it really used.

The runtime defines the scope vocabulary but deliberately does not measure
anything itself: measurement is an optional plugin concern, while *being
findable* is a property of the execution model and therefore belongs here.
"""

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ResourceRequest(BaseModel):
    """Advisory compute requirements for a task. Optional; consumed by
    resource-aware targets (e.g. Slurm, Terraform) when present.

    All fields are hints. Targets are free to round up, ignore unsupported
    fields, or reject a request they cannot satisfy. Unknown fields are
    rejected so typos in a workflow YAML surface as validation errors rather
    than being silently dropped.

    Attributes:
        cpus: Number of CPU cores to request. ``None`` lets the target choose.
        gpus: Number of GPUs to request. Defaults to ``0`` (no GPU).
        memory_gb: System RAM to request, in gibibytes. ``None`` lets the
            target choose.
        vram_gb: GPU memory to request per GPU, in gibibytes. ``None`` lets the
            target choose.
        walltime: Maximum wall-clock runtime, as a target-interpreted string
            (e.g. ``"01:30:00"``). ``None`` means no explicit limit.
    """

    model_config = ConfigDict(extra="forbid")

    cpus: int | None = Field(default=None, ge=1)
    gpus: int = Field(default=0, ge=0)
    memory_gb: int | None = Field(default=None, ge=1)
    vram_gb: int | None = Field(default=None, ge=1)
    walltime: str | None = None


@dataclass(frozen=True)
class ResourceScope:
    """
    Where a task's work actually runs, so an observer can find and measure it.

    An executor answers this once per task (see
    :meth:`~horus_runtime.core.executor.base.BaseExecutor.resource_scope`) and
    an observer — the resource-monitor plugin, a profiler, anything — decides
    what to do with the answer. The runtime itself never measures.

    Subclasses name a *kind of place*, not a kind of executor. That is the
    whole point: an observer that understands "a process tree" measures every
    executor that runs one, including executors written after the observer.
    """

    #: Where the work runs, for observers to dispatch on. Kept as a plain
    #: string rather than an enum so a plugin can introduce a scope the
    #: runtime has never heard of without a release here.
    kind: str

    #: Scope-specific detail. Free-form for the same reason.
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessTreeScope(ResourceScope):
    """
    The work is a process and its descendants, on the target's host.

    The overwhelmingly common answer, and the default. ``pid`` is the root of
    the tree; because the runtime spawns commands in a new session
    (``start_new_session=True`` locally, ``setsid`` when detached), it doubles
    as the process-group id, so the whole tree is reachable from it.

    ``pid`` may be ``None`` when the channel could not report one (see
    :attr:`~horus_runtime.core.target.channel.ChannelProcess.pid`). An
    observer that needs a pid should then fall back to a host-side method
    rather than treating it as an error.
    """

    kind: str = "process_tree"
    pid: int | None = None


@dataclass(frozen=True)
class ContainerScope(ResourceScope):
    """
    The work runs inside a container, not in the spawned process tree.

    A container runtime's CLI is a thin client: the real workload is reparented
    under the container daemon's supervisor in a different process group, so
    walking the spawned tree measures the client and nothing else. Executors
    that launch containers must say so by returning this, and identify the
    container so an observer can ask the daemon instead.
    """

    kind: str = "container"
    container_id: str | None = None
    #: Set when the id is not known yet but the runtime writes it to this path
    #: on the target once the container starts (e.g. ``docker --cidfile``).
    cidfile: str | None = None


@dataclass(frozen=True)
class InProcessScope(ResourceScope):
    """
    The work runs inside the orchestrator process itself.

    No separate process exists, so nothing can be attributed to this task
    alone: an observer can only report the orchestrator's own usage, which is
    shared with every other task in flight. Returning this is how an executor
    declares that any measurement of it is an estimate.
    """

    kind: str = "in_process"
    pid: int | None = None
