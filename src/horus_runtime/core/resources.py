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

- :class:`ResourceRequest`: what a task *asks for*. Advisory: resource-aware
  targets (Slurm, Terraform, etc.) translate the hints into their own
  provisioning primitives, and targets that ignore them are unaffected.
- :class:`ResourceScope`: where a task's work *actually runs*, so an observer
  can find it and measure what it really used.
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
    """

    #: Where the work runs, for observers to dispatch on.
    kind: str

    #: Scope-specific detail.
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessTreeScope(ResourceScope):
    """
    The work is a process and its descendants, on the target's host.
    """

    kind: str = "process_tree"
    pid: int | None = None


@dataclass(frozen=True)
class InProcessScope(ResourceScope):
    """
    The work runs inside the orchestrator process itself.
    """

    kind: str = "in_process"
    pid: int | None = None
