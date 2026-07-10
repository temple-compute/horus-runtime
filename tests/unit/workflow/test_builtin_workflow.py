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
Unit tests for the Workflow class.
"""

import textwrap
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import AsyncMock, patch

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.dag import UnknownTaskError
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.artifact.store import ArtifactStore
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.core.transfer.strategy import BaseTransferStrategy
from horus_runtime.core.workflow.edge import WorkflowEdge
from tests.conftest import (
    MakeMockSubprocessType,
    MakeTaskType,
    MakeWorkflowFileType,
)


@pytest.mark.unit
class TestWorkflowConstruction:
    """
    Tests for constructing Workflow instances.
    """

    def test_basic_construction(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Test that a workflow can be constructed from a valid YAML file.
        """
        wf_content = textwrap.dedent("""\
            name: test_workflow
            kind: horus_workflow
            tasks:
              - id: test_task_id
                name: task1
                kind: horus_task
                runtime:
                  kind: command
                  command: "echo hello"
                executor:
                  kind: shell
            """)

        wf_file = make_workflow_file(tmp_path, wf_content)
        wf = HorusWorkflow.from_yaml(wf_file)

        assert wf.name == "test_workflow"
        assert isinstance(wf, HorusWorkflow)
        assert wf.kind == "horus_workflow"
        assert "test_task_id" in [t.id for t in wf.tasks]

    def test_tasks_preserve_order(
        self, tmp_path: Path, make_workflow_file: MakeWorkflowFileType
    ) -> None:
        """
        Test that tasks are loaded in the order they are defined in the YAML
        file.
        """
        wf_content = textwrap.dedent("""\
            name: ordered
            kind: horus_workflow
            tasks:
              - id: task_a_id
                name: A
                kind: horus_task
                runtime:
                  kind: command
                  command: "echo A"
                executor:
                  kind: shell
              - id: task_b_id
                name: B
                kind: horus_task
                runtime:
                  kind: command
                  command: "echo B"
                executor:
                  kind: shell
              - id: task_c_id
                name: C
                kind: horus_task
                runtime:
                  kind: command
                  command: "echo C"
                executor:
                  kind: shell
            """)
        wf_file = make_workflow_file(tmp_path, wf_content)
        wf = HorusWorkflow.from_yaml(wf_file)
        assert [t.id for t in wf.tasks] == [
            "task_a_id",
            "task_b_id",
            "task_c_id",
        ]

    def test_empty_tasks(self) -> None:
        """
        Test that a workflow can be created with an empty tasks list.
        """
        wf = HorusWorkflow(name="empty", tasks=[])
        assert wf.tasks == []


@pytest.mark.unit
class TestOrchestratorWorkingDirectoryPropagation:
    """
    Tests that local (co-located) task targets inherit the orchestrator
    target's working directory.
    """

    def _make_task(self, task_id: str, target: LocalTarget) -> HorusTask:
        return HorusTask(
            id=task_id,
            name=task_id,
            runtime=CommandRuntime(command="echo hi"),
            executor=ShellExecutor(),
            target=target,
        )

    def test_local_task_inherits_orchestrator_working_directory(
        self, tmp_path: Path
    ) -> None:
        """
        A task whose local target left ``working_directory`` at its default
        runs under the orchestrator target's folder (nested by task id).
        """
        folder = tmp_path / "orchestrator_folder"
        task = self._make_task("worker", LocalTarget())
        wf = HorusWorkflow(
            name="propagation",
            tasks=[task],
            orchestrator_target=LocalTarget(
                working_directory=folder.as_posix()
            ),
        )

        wf._propagate_orchestrator_working_directory()

        assert task.target.working_directory == folder.as_posix()
        assert task.working_dir == (folder / "worker").as_posix()

    def test_explicit_task_working_directory_is_preserved(
        self, tmp_path: Path
    ) -> None:
        """
        A task target that set its own ``working_directory`` keeps it and is
        not overridden by the orchestrator folder.
        """
        orchestrator_folder = tmp_path / "orchestrator_folder"
        task_folder = tmp_path / "task_folder"
        task = self._make_task(
            "worker", LocalTarget(working_directory=task_folder.as_posix())
        )
        wf = HorusWorkflow(
            name="propagation_override",
            tasks=[task],
            orchestrator_target=LocalTarget(
                working_directory=orchestrator_folder.as_posix()
            ),
        )

        wf._propagate_orchestrator_working_directory()

        assert task.target.working_directory == task_folder.as_posix()
        assert task.working_dir == (task_folder / "worker").as_posix()

    def test_unset_orchestrator_folder_leaves_task_to_resolve_cwd(
        self,
    ) -> None:
        """
        When the orchestrator target has no working_directory, there is nothing
        to propagate: the local task stays unset and resolves to the CWD via
        ``LocalTarget.resolved_working_directory``.
        """
        task = self._make_task("worker", LocalTarget())
        wf = HorusWorkflow(
            name="propagation_noop",
            tasks=[task],
            orchestrator_target=LocalTarget(),
        )

        wf._propagate_orchestrator_working_directory()

        assert task.target.working_directory is None
        assert task.working_dir == (Path.cwd() / "worker").as_posix()


@pytest.mark.unit
class TestWorkflowRun:
    """
    Tests for the run method of the Workflow class.
    """

    async def test_run_executes_task_without_outputs(
        self,
        tmp_path: Path,
        make_workflow_file: MakeWorkflowFileType,
        horus_context: HorusContext,
    ) -> None:
        """
        Test that a workflow with a single task and no outputs executes the
        task.
        """
        del horus_context
        wf_contents = textwrap.dedent("""\
        name: run_test
        kind: horus_workflow
        tasks:
            - id: test_task_id
              name: Task
              kind: horus_task
              runtime:
                  kind: command
                  command: "echo test"
              executor:
                  kind: shell
        """)

        wf_file = make_workflow_file(tmp_path, wf_contents)
        wf = HorusWorkflow.from_yaml(wf_file)

        with patch.object(HorusTask, "run") as mock_run:
            await wf.run(trigger_id="test_task_id")

        mock_run.assert_awaited_once()

    async def test_run_skips_task_when_all_outputs_exist(
        self,
        tmp_path: Path,
        make_workflow_file: MakeWorkflowFileType,
        horus_context: HorusContext,
    ) -> None:
        """
        Test that a task is skipped if all its outputs already exist.
        """
        del horus_context
        wf_contents = textwrap.dedent("""\
            name: skip_test
            kind: horus_workflow
            tasks:
                - id: test_task_id
                  name: Task
                  kind: horus_task
                  skip_if_complete: True
                  runtime:
                      kind: command
                      command: "echo test"
                  executor:
                      kind: shell
                  outputs:
                      - id: output_file
                        kind: file
                        path: /tmp/some_file.txt
            """)
        wf_file = make_workflow_file(tmp_path, wf_contents)
        wf = HorusWorkflow.from_yaml(wf_file)

        with (
            patch.object(
                ArtifactStore,
                "exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(HorusTask, "_run") as mock_run,
        ):
            await wf.run(trigger_id="test_task_id")

        mock_run.assert_not_called()

    @patch("asyncio.create_subprocess_shell")
    async def test_run_executes_tasks_in_dependency_order(
        self,
        mock_run: AsyncMock,
        tmp_path: Path,
        horus_context: HorusContext,
        make_mock_subprocess: MakeMockSubprocessType,
    ) -> None:
        """
        Tasks execute in topological (dependency) order: a task that produces
        an artifact runs before the task that consumes it, regardless of the
        order in which they are listed in the workflow.
        """
        del horus_context
        mock_run.side_effect = [
            make_mock_subprocess(returncode=0),
            make_mock_subprocess(returncode=0),
        ]

        shared_path = tmp_path / "shared.txt"

        producer = HorusTask(
            id="producer",
            name="producer",
            outputs=[FileArtifact(id="shared", path=shared_path)],
            # Always run, even though its output is mocked to "exist".
            skip_if_complete=False,
            runtime=CommandRuntime(command="echo A"),
            executor=ShellExecutor(),
            target=LocalTarget(),
        )
        consumer = HorusTask(
            id="consumer",
            name="consumer",
            inputs=[FileArtifact(id="shared", path=shared_path)],
            runtime=CommandRuntime(command="echo B"),
            executor=ShellExecutor(),
            target=LocalTarget(),
        )

        # Consumer is listed first to prove ordering follows the dependency
        # graph (the edge), not the definition order.
        wf = HorusWorkflow(
            name="order_test",
            tasks=[consumer, producer],
            edges=[
                WorkflowEdge(
                    source="producer",
                    source_output="shared",
                    target="consumer",
                    target_input="shared",
                )
            ],
        )

        with patch.object(
            ArtifactStore, "exists", new_callable=AsyncMock, return_value=True
        ):
            await wf.run(trigger_id="producer")

        # Two tasks, two calls
        function_calls = 2

        assert mock_run.call_count == function_calls

        calls = [call.args[0] for call in mock_run.call_args_list]
        assert calls == ["echo A", "echo B"]

    @patch("asyncio.create_subprocess_shell")
    async def test_run_stops_on_first_failure(
        self,
        mock_run: AsyncMock,
        tmp_path: Path,
        horus_context: HorusContext,
        make_mock_subprocess: MakeMockSubprocessType,
    ) -> None:
        """
        If a task fails, the workflow should stop and not
        execute downstream tasks that depend on it.
        """
        del horus_context

        mock_run.return_value = make_mock_subprocess(returncode=1)

        class TaskWithFailure(HorusTask):
            add_to_registry: ClassVar[bool] = False

            async def _run(self) -> None:
                await super()._run()  # Here it calls +1 run count
                raise TaskExecutionError("fail")

        shared_path = tmp_path / "shared.txt"

        task_a = TaskWithFailure(
            id="task_a",
            name="task_a",
            inputs=[],
            outputs=[FileArtifact(id="shared", path=shared_path)],
            skip_if_complete=False,
            runtime=CommandRuntime(command="echo A"),
            executor=ShellExecutor(),
            target=LocalTarget(),
        )
        # task_b depends on task_a's output, so it is downstream of task_a.
        task_b = HorusTask(
            id="task_b",
            name="task_b",
            inputs=[FileArtifact(id="shared", path=shared_path)],
            runtime=CommandRuntime(command="echo B"),
            executor=ShellExecutor(),
            target=LocalTarget(),
        )

        wf = HorusWorkflow(
            name="stop_test",
            tasks=[task_a, task_b],
            edges=[
                WorkflowEdge(
                    source="task_a",
                    source_output="shared",
                    target="task_b",
                    target_input="shared",
                )
            ],
        )
        with pytest.raises(TaskExecutionError):
            await wf.run(trigger_id="task_a")

        assert task_a.runs == 1
        assert task_b.runs == 0

    async def test_run_binds_task_to_target_before_transfer(
        self,
        tmp_path: Path,
        horus_context: HorusContext,
    ) -> None:
        """
        _run must call ``task.target.bind(task)`` before transferring
        artifacts so resource-aware targets that provision lazily at transfer
        time can read ``task.resources``. ``_task`` is otherwise only set by
        dispatch, which runs *after* transfer, so observing it during transfer
        proves bind ran first.
        """
        del horus_context
        task = HorusTask(
            id="bind_task",
            name="bind_task",
            runtime=CommandRuntime(command="echo hi"),
            executor=ShellExecutor(),
            target=LocalTarget(working_directory=tmp_path.as_posix()),
        )
        wf = HorusWorkflow(name="bind_order", tasks=[task])

        recorded: dict[str, bool] = {}

        async def _recording_transfer(
            self: HorusWorkflow,
            target_task: HorusTask,
            source_map: object = None,
        ) -> None:
            del self, source_map
            recorded["bound_before_transfer"] = (
                target_task.target._task is target_task
            )

        # Patch on the class: pydantic models forbid setattr of non-field
        # attributes on instances, so an instance-level patch.object fails.
        with (
            patch.object(
                HorusWorkflow, "transfer_artifacts", _recording_transfer
            ),
            patch.object(HorusTask, "run", new=AsyncMock()),
        ):
            await wf.run(trigger_id="bind_task")

        assert recorded["bound_before_transfer"] is True

    async def test_run_empty_workflow_raises_for_missing_trigger(
        self, horus_context: HorusContext
    ) -> None:
        """
        An empty workflow has no task to trigger, so running it with any
        trigger id raises UnknownTaskError.
        """
        del horus_context
        wf = HorusWorkflow(name="empty", tasks=[])

        with pytest.raises(UnknownTaskError):
            await wf.run(trigger_id="test_task_id")

    async def test_run_raises_for_unknown_trigger(
        self, make_shell_task: MakeTaskType, horus_context: HorusContext
    ) -> None:
        """
        Running a workflow with a trigger id that does not match any task
        raises UnknownTaskError.
        """
        del horus_context
        task = make_shell_task(cmd="echo test")

        wf = HorusWorkflow(name="unknown_trigger", tasks=[task])

        with pytest.raises(UnknownTaskError):
            await wf.run(trigger_id="does_not_exist")


@pytest.mark.unit
class TestTransferArtifactsProducerMap:
    """
    Tests for ``transfer_artifacts`` source resolution. The producer map must
    cover every task in the workflow, not just those defined before the task
    being dispatched, because tasks may execute in topological (DAG) order that
    differs from definition order.
    """

    @staticmethod
    def _make_task(
        *,
        task_id: str,
        inputs: list[FileArtifact],
        outputs: list[FileArtifact],
        target: LocalTarget,
    ) -> HorusTask:
        return HorusTask(
            id=task_id,
            name=task_id,
            inputs=cast(list[BaseArtifact], inputs),
            outputs=cast(list[BaseArtifact], outputs),
            runtime=CommandRuntime(command="echo hi"),
            executor=ShellExecutor(),
            target=target,
        )

    async def test_producer_defined_after_consumer_resolves_to_producer(
        self, tmp_path: Path
    ) -> None:
        """
        When a producer task is defined *after* its consumer in ``tasks`` (a
        valid DAG ordering), the consumer's input artifact must resolve to the
        producer's target, not be misclassified as a root input sourced from
        ``orchestrator_target``.
        """
        producer_target = LocalTarget()
        consumer_target = LocalTarget()
        artifact_path = tmp_path / "x.txt"

        producer = self._make_task(
            task_id="producer",
            inputs=[],
            outputs=[FileArtifact(id="art_x", path=artifact_path)],
            target=producer_target,
        )
        consumer = self._make_task(
            task_id="consumer",
            inputs=[FileArtifact(id="art_x", path=artifact_path)],
            outputs=[],
            target=consumer_target,
        )

        # Consumer is listed BEFORE its producer on purpose.
        wf = HorusWorkflow(
            name="dag_order",
            tasks=[consumer, producer],
            edges=[
                WorkflowEdge(
                    source="producer",
                    source_output="art_x",
                    target="consumer",
                    target_input="art_x",
                )
            ],
        )

        captured: dict[str, object] = {}

        class _RecordingStrategy:
            async def transfer(
                self, artifact: object, source: object, dest: object
            ) -> None:
                del artifact
                captured["source"] = source
                captured["dest"] = dest

        with patch.object(
            BaseTransferStrategy,
            "get_from_registry",
            return_value=_RecordingStrategy,
        ) as mock_get:
            await wf.transfer_artifacts(consumer)

        # The source must be the producer's target — proving the producer was
        # found in the map despite being defined after the consumer.
        assert captured["source"] is producer_target
        assert captured["dest"] is consumer_target
        assert mock_get.call_args.args[0] is producer_target
        # And specifically not the orchestrator fallback.
        assert captured["source"] is not wf.orchestrator_target

    async def test_input_without_producer_falls_back_to_orchestrator(
        self, tmp_path: Path
    ) -> None:
        """
        A genuine root input (no producing task) still resolves to
        ``orchestrator_target``.
        """
        consumer_target = LocalTarget()
        consumer = self._make_task(
            task_id="consumer",
            inputs=[FileArtifact(id="root_art", path=tmp_path / "r.txt")],
            outputs=[],
            target=consumer_target,
        )
        wf = HorusWorkflow(name="root_only", tasks=[consumer])

        captured: dict[str, object] = {}

        class _RecordingStrategy:
            async def transfer(
                self, artifact: object, source: object, dest: object
            ) -> None:
                del artifact, dest
                captured["source"] = source

        with patch.object(
            BaseTransferStrategy,
            "get_from_registry",
            return_value=_RecordingStrategy,
        ):
            await wf.transfer_artifacts(consumer)

        assert captured["source"] is wf.orchestrator_target
