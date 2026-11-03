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
HorusWorkflow implementation for Horus built-in workflows.
"""

from typing import ClassVar, Self

from pydantic import model_validator

from horus_builtin.target.local import LocalTarget
from horus_builtin.workflow.map import MapConfigurationError, MapTask
from horus_builtin.workflow.scheduler import run_schedule
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.core.workflow.edge import WorkflowEdge
from horus_runtime.i18n import tr as _


class HorusWorkflow(BaseWorkflow):
    """
    Basic implementation of the Workflow class for Horus built-in workflows.

    The workflow determines whether each task needs to run by inspecting the
    existence of its declared output artifacts. Tasks that have already
    produced all their outputs are skipped, which provides basic incremental
    execution: re-running a workflow only re-executes tasks whose outputs are
    missing.
    """

    kind: str = "horus_workflow"
    kind_name: ClassVar[str] = "Horus Workflow"
    kind_description: ClassVar[str] = _(
        "The default workflow implementation for Horus built-in workflows."
    )

    orchestrator_target: BaseTarget | None = LocalTarget()
    """
    The orchestrator runs locally; root input artifacts (those not produced by
    any upstream task) are sourced from the local filesystem.
    """

    @model_validator(mode="after")
    def check_map_pairing(self) -> Self:  # noqa: PLR0915
        """
        Validate what a map's chain and zip need from its edges: a chained
        or paired input must come from a map over the same items, and a
        chained source feeds only its paired input.
        """
        producers = {
            (edge.target, edge.target_input): edge
            for edge in self.edges
            if edge.transfer and edge.target_input is not None
        }
        maps = {t.id: t for t in self.tasks if isinstance(t, MapTask)}
        into: dict[str, list[WorkflowEdge]] = {}
        for edge in self.edges:
            into.setdefault(edge.target, []).append(edge)
        spaces: dict[str, tuple[str | None, str] | None] = {}

        def map_source(task: MapTask, input_id: str) -> MapTask | None:
            edge = producers.get((task.id, input_id))
            return maps.get(edge.source) if edge is not None else None

        def head_space(task: MapTask) -> tuple[str | None, str]:
            # The head's producer names the item space.
            edge = producers.get((task.id, task.over.input_id))
            if edge is None:
                return task.id, task.over.input_id
            if edge.source.startswith("artifact-"):
                return None, str(edge.source_output)
            return edge.source, str(edge.source_output)

        def space(task: MapTask) -> tuple[str | None, str] | None:
            # Follow `over` while a map feeds it, in a loop: a chain can be
            # deeper than the recursion limit. None on a cycle: the cycle
            # check reports it.
            path: list[str] = []
            current = task
            while current.id not in spaces:
                if current.id in path:
                    spaces[current.id] = None
                    break
                path.append(current.id)
                source = map_source(current, current.over.input_id)
                if source is None:
                    spaces[current.id] = head_space(current)
                    break
                current = source
            found = spaces[current.id]
            for task_id in path:
                spaces[task_id] = found
            return found

        def over_source(task: MapTask) -> str:
            edge = producers.get((task.id, task.over.input_id))
            if edge is None:
                return task.over.input_id
            if edge.source.startswith("artifact-") and edge.source_output:
                return edge.source_output
            return edge.source

        for task in maps.values():
            streamed: set[str] = set()
            if task.chain:
                source = map_source(task, task.over.input_id)
                if source is None:
                    raise MapConfigurationError(
                        _(
                            "Map task '%(id)s' is chained, but '%(input)s' "
                            "does not come from a map."
                        )
                        % {"id": task.id, "input": task.over.input_id}
                    )
                streamed.add(source.id)
            for entry in task.zip:
                source = map_source(task, entry.input_id)
                if source is None:
                    raise MapConfigurationError(
                        _(
                            "Map task '%(id)s' pairs '%(input)s' by item, but "
                            "it does not come from a map."
                        )
                        % {"id": task.id, "input": entry.input_id}
                    )
                spaces = space(source), space(task)
                if None not in spaces and spaces[0] != spaces[1]:
                    raise MapConfigurationError(
                        _(
                            "Map task '%(id)s' pairs '%(input)s' by item, but "
                            "'%(source)s' maps over different items than "
                            "'%(over_source)s'."
                        )
                        % {
                            "id": task.id,
                            "input": entry.input_id,
                            "source": source.id,
                            "over_source": over_source(task),
                        }
                    )
                if task.chain:
                    streamed.add(source.id)
            # A chained map starts before its source finishes, so a
            # condition on that edge, or one reading that source, would be
            # read too early.
            for edge in into.get(task.id, []):
                read = getattr(edge.condition, "source_task", None)
                early = {edge.source, read} & streamed
                if edge.condition and early:
                    raise MapConfigurationError(
                        _(
                            "Map task '%(id)s' is chained to '%(source)s', so "
                            "its edge cannot have a condition."
                        )
                        % {"id": task.id, "source": min(early)}
                    )
            paired = {task.over.input_id, *(e.input_id for e in task.zip)}
            for edge in into.get(task.id, []):
                if (
                    edge.transfer
                    and edge.source in streamed
                    and edge.target_input not in paired
                ):
                    raise MapConfigurationError(
                        _(
                            "Map task '%(id)s' is chained to '%(source)s', so "
                            "no other input can take all of its output."
                        )
                        % {"id": task.id, "source": edge.source}
                    )
        return self

    async def _run(self, trigger_id: str) -> None:
        """
        Dispatch tasks via the concurrent ready-set scheduler.

        A task is skipped when all of its output artifacts exist (see
        :meth:`is_complete`). Every task whose dependencies are satisfied
        runs as soon as it is ready, concurrently with any other ready task
        (bounded by :attr:`max_concurrency` when set); see
        :func:`horus_builtin.workflow.scheduler.run_schedule` for the
        scheduling loop itself.
        """
        await run_schedule(self, trigger_id)

    async def _reset(self) -> None:
        """
        Reset the workflow by deleting all output artifacts of all tasks in the
        workflow. This allows the workflow to be re-run from scratch.
        """
        for task in self.tasks:
            await task.reset()
