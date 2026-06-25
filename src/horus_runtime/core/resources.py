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
Portable, target-agnostic compute resource requests for tasks.

The model defined here is advisory: a task may declare the resources it would
like, and resource-aware targets (Slurm, Terraform, etc.) translate those hints
into their own provisioning primitives. Targets that ignore resources are
unaffected, which keeps the field fully backwards compatible.
"""

from pydantic import BaseModel, ConfigDict


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

    cpus: int | None = None
    gpus: int = 0
    memory_gb: int | None = None
    vram_gb: int | None = None
    walltime: str | None = None
