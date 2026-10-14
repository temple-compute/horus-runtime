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
Tests for a workflow's named targets (``targets:``) and the
``{ref: <name>}`` task targets that share them.
"""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_builtin.workflow.loop import loop_task
from horus_builtin.workflow.subworkflow.expander import SubworkflowExpander
from horus_runtime.context import HorusContext
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.workflow.exceptions import TargetRefInLoopError
from horus_runtime.secrets import Secret, iter_secret_fields

HUNTER2 = "hunter2"  # test fixture value, not a real secret


class _PasswordTarget(LocalTarget):
    """A target with a secret field and a one-CPU member rule."""

    kind: str = "pw_target"
    password: Secret | None = None
    cpus: int | None = None

    def validate_member(self, task: BaseTask) -> list[str]:
        wanted = task.resources.cpus if task.resources else None
        if self.cpus is not None and wanted and wanted > self.cpus:
            return [f"Task '{task.id}' needs {wanted} cpus."]
        return []

    def validate_named(self) -> list[str]:
        return (
            [] if self.cpus else [f"Shared machine '{self._ref}' needs cpus."]
        )


def _task(task_id: str, target: Any, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "horus_task",
        "id": task_id,
        "name": task_id,
        "executor": {"kind": "shell"},
        "runtime": {"kind": "command", "command": "true"},
        "target": target,
        **extra,
    }


def _doc(tasks: list[Any], targets: Any = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "kind": "horus_workflow",
        "name": "wf",
        "tasks": tasks,
    }
    if targets is not None:
        doc["targets"] = targets
    return doc


HEAVY = {"heavy": {"kind": "pw_target", "cpus": 8}}


@pytest.mark.unit
class TestResolution:
    """``{ref}`` resolves to the one declared instance."""

    def test_tasks_with_the_same_ref_share_one_instance(self) -> None:
        """Tasks with the same ref share one instance."""
        wf = HorusWorkflow.model_validate(
            _doc(
                [_task("a", {"ref": "heavy"}), _task("b", {"ref": "heavy"})],
                HEAVY,
            )
        )

        a, b = wf.tasks
        assert a.target is b.target is wf.targets["heavy"]
        assert a.target._ref == "heavy"

    def test_inline_targets_are_unchanged(self) -> None:
        """Inline targets are unchanged."""
        wf = HorusWorkflow.model_validate(
            _doc([_task("a", {"kind": "local"})], HEAVY)
        )

        assert wf.tasks[0].target._ref is None
        assert wf.tasks[0].target is not wf.targets["heavy"]

    @pytest.mark.parametrize(
        ("targets", "task_target", "message"),
        [
            (HEAVY, {"ref": "light"}, "refers to unknown target 'light'"),
            (None, {"ref": "light"}, "refers to unknown target 'light'"),
            ({"Heavy": {"kind": "local"}}, {"kind": "local"}, "lower-case"),
            ({"x": {"ref": "y"}}, {"kind": "local"}, "cannot itself be a"),
            (HEAVY, {"ref": "heavy", "extra": 1}, "Extra inputs"),
            (HEAVY, {"ref": "Bad Name"}, "should match pattern"),
            (HEAVY, {"ref": "heavy", "kind": "local"}, "Extra inputs"),
        ],
    )
    def test_invalid_documents_are_rejected(
        self, targets: Any, task_target: Any, message: str
    ) -> None:
        """Invalid documents are rejected."""
        with pytest.raises(ValidationError, match=message):
            HorusWorkflow.model_validate(
                _doc([_task("a", task_target)], targets)
            )

    def test_ref_in_a_loop_body_is_rejected(self) -> None:
        """Ref in a loop body is rejected."""
        loop = {
            "id": "lp",
            "loop": {
                "body": _task("body", {"ref": "heavy"}),
                "until": "signal",
                "max_iterations": 2,
            },
        }

        with pytest.raises(ValidationError, match="inside loop bodies"):
            HorusWorkflow.model_validate(_doc([loop], HEAVY))

    def test_ref_in_a_subworkflow_inside_a_loop_is_rejected(self) -> None:
        """Rejected at load time, not at the iteration that inlines it."""
        body = _doc([_task("inner", {"ref": "heavy"})])
        loop = {
            "id": "lp",
            "loop": {
                "body": {"id": "sub", "sub": body},
                "until": "signal",
                "max_iterations": 2,
            },
        }

        with pytest.raises(ValidationError, match="inside loop bodies"):
            HorusWorkflow.model_validate(_doc([loop], HEAVY))

    def test_member_and_named_checks_surface(self) -> None:
        """Member and named checks surface."""
        big = _task("a", {"ref": "heavy"}, resources={"cpus": 64})
        with pytest.raises(ValidationError, match="needs 64 cpus"):
            HorusWorkflow.model_validate(_doc([big], HEAVY))

        unsized = {"heavy": {"kind": "pw_target"}}
        with pytest.raises(ValidationError, match="'heavy' needs cpus"):
            HorusWorkflow.model_validate(
                _doc([_task("a", {"ref": "heavy"})], unsized)
            )

    def test_full_target_with_a_ref_is_rejected(self) -> None:
        """A target with ``kind`` and ``ref`` is rejected outside a doc too."""
        with pytest.raises(ValidationError, match="cannot also have 'ref'"):
            HorusTask.model_validate(
                _task("a", {"kind": "local", "ref": "heavy"})
            )

    def test_target_schema_accepts_a_ref(self) -> None:
        """The task ``target`` JSON schema offers ``{ref}`` too."""
        target = HorusTask.model_json_schema()["properties"]["target"]

        refs = [s for s in target["anyOf"] if "ref" in s.get("properties", {})]
        assert refs[0]["required"] == ["ref"]
        assert any(s.get("required") == ["kind"] for s in target["anyOf"])

    def test_target_schema_rejects_a_ref_with_target_fields(self) -> None:
        """No ``anyOf`` branch takes ``{ref, kind}``, as the runtime does."""
        target = HorusTask.model_json_schema()["properties"]["target"]

        full = [
            s for s in target["anyOf"] if "ref" not in s.get("properties", {})
        ]
        assert full[0]["not"] == {"required": ["ref"]}

    def test_loop_task_rejects_a_named_body(self) -> None:
        """``wf.loop`` with a body on a named target fails at once."""
        wf = HorusWorkflow.model_validate(
            _doc([_task("a", {"ref": "heavy"})], HEAVY)
        )
        with pytest.raises(TargetRefInLoopError):
            loop_task(
                wf, id="l", body=wf.tasks[0], until="out", max_iterations=2
            )

    def test_standalone_task_ref_has_a_clear_error(self) -> None:
        """Standalone task ref has a clear error."""
        with pytest.raises(ValidationError, match="can only be resolved"):
            BaseTask.model_validate(_task("a", {"ref": "heavy"}))

    def test_task_ids_may_not_start_with_at(self) -> None:
        """Task ids may not start with at."""
        with pytest.raises(ValidationError, match="should match pattern"):
            HorusWorkflow.model_validate(
                _doc([_task("@node", {"kind": "local"})])
            )


@pytest.mark.unit
class TestSerialization:
    """A dump keeps the sharing."""

    def test_dump_emits_refs_and_round_trips_identity(self) -> None:
        """Dump emits refs and round trips identity."""
        wf = HorusWorkflow.model_validate(
            _doc(
                [_task("a", {"ref": "heavy"}), _task("b", {"ref": "heavy"})],
                HEAVY,
            )
        )

        dumped = wf.model_dump(mode="json")

        assert [t["target"] for t in dumped["tasks"]] == [{"ref": "heavy"}] * 2
        assert dumped["targets"]["heavy"]["kind"] == "pw_target"
        again = HorusWorkflow.model_validate(dumped)
        assert again.tasks[0].target is again.tasks[1].target

    def test_inline_targets_context_inlines(self) -> None:
        """Inline targets context inlines."""
        wf = HorusWorkflow.model_validate(
            _doc([_task("a", {"ref": "heavy"})], HEAVY)
        )

        dumped = wf.model_dump(mode="json", context={"inline_targets": True})

        assert dumped["tasks"][0]["target"]["kind"] == "pw_target"

    def test_secret_is_yielded_once_under_targets(
        self, tmp_path: Path
    ) -> None:
        """Secret is yielded once under targets."""
        targets = {
            "heavy": {"kind": "pw_target", "cpus": 2, "password": HUNTER2}
        }
        wf = HorusWorkflow.model_validate(
            _doc(
                [_task("a", {"ref": "heavy"}), _task("b", {"ref": "heavy"})],
                targets,
            )
        )

        assert [p for p, _ in iter_secret_fields(wf)] == [
            "targets.heavy.password"
        ]
        out = tmp_path / "wf.yaml"
        wf.to_yaml(out)
        text = out.read_text()
        assert HUNTER2 not in text
        written = yaml.safe_load(text)
        assert written["tasks"][0]["target"] == {"ref": "heavy"}
        assert written["targets"]["heavy"]["password"].startswith("${secret:")


@pytest.mark.unit
class TestCopies:
    """Copies of a target and the machine identity they carry."""

    def test_idle_copy_shares_the_lease_key(self) -> None:
        """Idle copy shares the lease key."""
        target = LocalTarget()
        assert target.idle_copy().lease_key == target.lease_key

    def test_deep_copy_is_another_machine(self) -> None:
        """Deep copy is another machine."""
        target = LocalTarget()
        assert deepcopy(target).lease_key != target.lease_key


def _sub(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"id": task_id, "sub": body}


@pytest.mark.unit
class TestSubworkflowScope:
    """A subworkflow body sees its enclosing workflows' targets."""

    def test_body_ref_to_parent_target_is_the_parent_instance(
        self, tmp_path: Path
    ) -> None:
        """Body ref to parent target is the parent instance."""
        body = _doc([_task("inner", {"ref": "heavy"})])
        wf = HorusWorkflow.model_validate(
            _doc([_task("outer", {"ref": "heavy"}), _sub("sub", body)], HEAVY)
        )
        expander = wf.tasks[1]
        assert isinstance(expander, SubworkflowExpander)

        (inner,) = expander._build_inner_tasks(tmp_path)

        assert inner.id == "sub/inner"
        assert inner.target is wf.targets["heavy"]

    def test_inner_name_shadows_the_outer_one(self, tmp_path: Path) -> None:
        """Inner name shadows the outer one."""
        body = _doc(
            [_task("inner", {"ref": "heavy"})],
            {"heavy": {"kind": "pw_target", "cpus": 1}},
        )
        wf = HorusWorkflow.model_validate(_doc([_sub("sub", body)], HEAVY))
        expander = wf.tasks[0]
        assert isinstance(expander, SubworkflowExpander)

        (inner,) = expander._build_inner_tasks(tmp_path)

        assert inner.target is expander.body.targets["heavy"]
        assert inner.target is not wf.targets["heavy"]

    def test_inlined_inline_targets_are_separate_machines(
        self, tmp_path: Path
    ) -> None:
        """Two expansions of one body must not share an inline target."""
        body = _doc([_task("inner", {"kind": "local"})])
        wf = HorusWorkflow.model_validate(_doc([_sub("sub", body)]))
        expander = wf.tasks[0]
        assert isinstance(expander, SubworkflowExpander)

        (first,) = expander._build_inner_tasks(tmp_path)
        (second,) = expander._build_inner_tasks(tmp_path)

        assert first.target.lease_key != second.target.lease_key

    def test_unknown_ref_in_a_body_is_rejected(self) -> None:
        """Unknown ref in a body is rejected."""
        body = _doc([_task("inner", {"ref": "light"})])
        with pytest.raises(ValidationError, match="unknown target 'light'"):
            HorusWorkflow.model_validate(_doc([_sub("sub", body)], HEAVY))

    def test_nested_sub_sees_the_grandparent_targets(self) -> None:
        """A raw ``sub:`` inside a body resolves refs to the outer scope."""
        nested = _doc([_task("leaf", {"ref": "heavy"})])
        body = _doc([_sub("nested", nested)])
        wf = HorusWorkflow.model_validate(_doc([_sub("sub", body)], HEAVY))

        outer = wf.tasks[0]
        assert isinstance(outer, SubworkflowExpander)
        inner = outer.body.tasks[0]
        assert isinstance(inner, SubworkflowExpander)
        assert inner.body.tasks[0].target is wf.targets["heavy"]

    def test_expanding_keeps_a_nested_body_on_the_named_target(
        self, tmp_path: Path
    ) -> None:
        """Inlining a nested subworkflow does not fork its named target."""
        nested = _doc([_task("leaf", {"ref": "heavy"})])
        body = _doc([_sub("nested", nested)])
        wf = HorusWorkflow.model_validate(_doc([_sub("sub", body)], HEAVY))
        outer = wf.tasks[0]
        assert isinstance(outer, SubworkflowExpander)

        (inner,) = outer._build_inner_tasks(tmp_path)

        assert isinstance(inner, SubworkflowExpander)
        assert inner.body.tasks[0].target is wf.targets["heavy"]

    @pytest.mark.parametrize("name", ["inner", "heavy"])
    async def test_snapshot_after_expansion_keeps_body_targets(
        self, tmp_path: Path, horus_context: HorusContext, name: str
    ) -> None:
        """
        A dump taken after a body with its own target (new, or shadowing a
        parent name) was inlined reloads onto that same machine.
        """
        del horus_context
        body = _doc(
            [_task("inner", {"ref": name})],
            {name: {"kind": "pw_target", "cpus": 1}},
        )
        doc = _doc([_sub("sub", body)], HEAVY)
        doc["orchestrator_target"] = {
            "kind": "local",
            "working_directory": tmp_path.as_posix(),
        }
        wf = HorusWorkflow.model_validate(doc)

        await wf.run(trigger_id="sub")
        again = HorusWorkflow.model_validate(wf.model_dump(mode="json"))

        task = next(t for t in again.tasks if t.id == "sub/inner")
        ref = task.target._ref
        assert ref is not None
        assert task.target is again.targets[ref]
        assert task.target.cpus == 1  # type: ignore[attr-defined]
        assert again.targets["heavy"].cpus == 8  # type: ignore[attr-defined]

    @pytest.mark.parametrize("name", ["inner", "heavy"])
    async def test_rerun_from_a_snapshot_keeps_the_machine_names(
        self, tmp_path: Path, horus_context: HorusContext, name: str
    ) -> None:
        """
        Re-running from a dump does not hoist the body's machine again
        under a new name each time.
        """
        del horus_context
        body = _doc(
            [_task("inner", {"ref": name})],
            {name: {"kind": "pw_target", "cpus": 1}},
        )
        doc = _doc([_sub("sub", body)], HEAVY)
        doc["orchestrator_target"] = {
            "kind": "local",
            "working_directory": tmp_path.as_posix(),
        }
        wf = HorusWorkflow.model_validate(doc)
        await wf.run(trigger_id="sub")
        names = sorted(wf.targets)

        for _ in range(2):
            wf = HorusWorkflow.model_validate(wf.model_dump(mode="json"))
            await wf.run(trigger_id="sub")

        assert sorted(wf.targets) == names
        task = next(t for t in wf.tasks if t.id == "sub/inner")
        assert task.target.cpus == 1  # type: ignore[attr-defined]

    async def test_hoisted_rename_skips_names_of_nested_bodies(
        self, tmp_path: Path, horus_context: HorusContext
    ) -> None:
        """
        A renamed body machine does not take a name a nested body declares,
        so a reloaded dump keeps each task on its own machine.
        """
        del horus_context
        nested = _doc(
            [
                _task("leaf", {"ref": "heavy"}),
                _task("own", {"ref": "heavy-2"}),
            ],
            {"heavy-2": {"kind": "pw_target", "cpus": 2}},
        )
        body = _doc(
            [_task("top", {"ref": "heavy"}), _sub("s2", nested)],
            {"heavy": {"kind": "pw_target", "cpus": 1}},
        )
        doc = _doc([_sub("s1", body)], HEAVY)
        doc["orchestrator_target"] = {
            "kind": "local",
            "working_directory": tmp_path.as_posix(),
        }
        wf = HorusWorkflow.model_validate(doc)
        await wf.run(trigger_id="s1")

        again = HorusWorkflow.model_validate(wf.model_dump(mode="json"))

        s2 = next(t for t in again.tasks if t.id == "s1/s2")
        assert isinstance(s2, SubworkflowExpander)
        cpus = {t.id: t.target.cpus for t in s2.body.tasks}  # type: ignore[attr-defined]
        assert cpus == {"leaf": 1, "own": 2}

    def test_dumped_parent_round_trips_the_body_refs(self) -> None:
        """Dumped parent round trips the body refs."""
        body = _doc([_task("inner", {"ref": "heavy"})])
        wf = HorusWorkflow.model_validate(_doc([_sub("sub", body)], HEAVY))

        again = HorusWorkflow.model_validate(wf.model_dump(mode="json"))
        expander = again.tasks[0]
        assert isinstance(expander, SubworkflowExpander)
        assert expander.body.tasks[0].target is again.targets["heavy"]


@pytest.mark.unit
class TestPythonConstruction:
    """Building a workflow from objects names the shared instance."""

    def test_instance_under_targets_gets_its_name(self) -> None:
        """Instance under targets gets its name."""
        shared: BaseTarget = LocalTarget()
        task = HorusTask.model_validate(_task("a", {"kind": "local"}))
        task.target = shared

        wf = HorusWorkflow(name="wf", targets={"box": shared}, tasks=[task])

        assert wf.tasks[0].target._ref == "box"
        assert wf.model_dump(mode="json")["tasks"][0]["target"] == {
            "ref": "box"
        }
