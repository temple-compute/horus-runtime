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
Declarative subworkflow (composition) construct.

A ``subworkflow`` task carries a *complete* child workflow document in its
``body`` and, when it runs, inlines that child's tasks and edges into the
parent's live DAG. It is expressed either as a ``sub:`` block in YAML
(lowered by :func:`~horus_builtin.workflow.subworkflow.lowering.
lower_subworkflow_entry`, hooked into
:class:`~horus_runtime.core.workflow.base.BaseWorkflow`'s ``model_validate``
pipeline) or via the :func:`~horus_builtin.workflow.subworkflow.expander.
subworkflow_task` Python builder (``wf.subworkflow(...)``).
"""

from horus_builtin.workflow.subworkflow.errors import SubworkflowError
from horus_builtin.workflow.subworkflow.lowering import lower_subworkflow_entry
from horus_builtin.workflow.subworkflow.ports import (
    SubworkflowPort,
    derive_ports,
)

__all__ = [
    "SubworkflowError",
    "SubworkflowPort",
    "derive_ports",
    "lower_subworkflow_entry",
]
