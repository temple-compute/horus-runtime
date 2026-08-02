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
Tests for resolving ``PythonFunctionRuntime.func`` from a dotted path, which
is what makes a function task expressible in YAML.
"""

from pathlib import Path

import pytest
import yaml

from horus_builtin.runtime.python import PythonFunctionRuntime, import_callable
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.function import FunctionTask
from horus_builtin.workflow.horus_workflow import HorusWorkflow
from horus_runtime.context import HorusContext
from horus_runtime.core.workflow.base import BaseWorkflow

MODULE_SOURCE = """
from pathlib import Path

NOT_CALLABLE = 42


def work():
    Path("ran.txt").write_text("ok")


class Steps:
    @staticmethod
    def nested():
        return None
"""


@pytest.fixture
def function_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """
    Write an importable module of task functions and put it on ``sys.path``,
    the way a user's own workflow package would be.
    """
    name = f"horus_test_functions_{abs(hash(tmp_path)) % 10**8}"
    (tmp_path / f"{name}.py").write_text(MODULE_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    return name


@pytest.mark.unit
class TestImportCallable:
    """Resolution rules and error messages."""

    def test_colon_form(self, function_module: str) -> None:
        """``module:function`` resolves."""
        assert import_callable(f"{function_module}:work").__name__ == "work"

    def test_dotted_form(self, function_module: str) -> None:
        """``module.function`` resolves too."""
        assert import_callable(f"{function_module}.work").__name__ == "work"

    def test_attribute_chain(self, function_module: str) -> None:
        """A reference may walk into a class."""
        resolved = import_callable(f"{function_module}:Steps.nested")
        assert resolved() is None

    def test_unknown_module_names_the_module(self) -> None:
        """A missing module is reported by name, not as a bare ImportError."""
        with pytest.raises(ValueError, match="no_such_module_here"):
            import_callable("no_such_module_here:work")

    def test_unknown_attribute_names_the_attribute(
        self, function_module: str
    ) -> None:
        """A typo in the function name says which attribute was missing."""
        with pytest.raises(ValueError, match="typo"):
            import_callable(f"{function_module}:typo")

    def test_non_callable_is_rejected(self, function_module: str) -> None:
        """Pointing at a constant is an error, not a runtime surprise."""
        with pytest.raises(ValueError, match="non-callable"):
            import_callable(f"{function_module}:NOT_CALLABLE")

    def test_malformed_reference(self) -> None:
        """A bare name cannot name both a module and an attribute."""
        with pytest.raises(ValueError, match="Invalid function reference"):
            import_callable("work")


@pytest.mark.unit
class TestPythonFunctionRuntimeFunc:
    """The runtime field itself."""

    def test_accepts_a_real_callable_unchanged(self) -> None:
        """Passing a function keeps working exactly as before."""

        def work() -> None:
            return None

        assert PythonFunctionRuntime(func=work).func is work

    def test_accepts_a_reference_string(self, function_module: str) -> None:
        """A dotted path is resolved at validation time."""
        runtime = PythonFunctionRuntime(
            func=f"{function_module}:work"  # type: ignore[arg-type]
        )
        assert callable(runtime.func)

    def test_serializes_back_to_a_reference(
        self, function_module: str
    ) -> None:
        """The dump is the string that imports the function again."""
        runtime = PythonFunctionRuntime(
            func=f"{function_module}:work"  # type: ignore[arg-type]
        )
        assert runtime.model_dump(mode="json")["func"] == (
            f"{function_module}:work"
        )


@pytest.mark.unit
class TestYamlRoundTrip:
    """A function task written as YAML loads and runs."""

    async def test_round_trip_and_run(
        self,
        tmp_path: Path,
        function_module: str,
        horus_context: HorusContext,
    ) -> None:
        """``to_yaml`` -> ``from_yaml`` -> ``run`` on a dotted-path task."""
        del horus_context
        wf = HorusWorkflow(
            name="wf",
            orchestrator_target=LocalTarget(
                working_directory=tmp_path.as_posix()
            ),
        )
        wf.tasks.append(
            FunctionTask(
                id="step",
                name="step",
                runtime=PythonFunctionRuntime(
                    func=f"{function_module}:work"  # type: ignore[arg-type]
                ),
            )
        )

        out_path = tmp_path / "workflow.yaml"
        wf.to_yaml(out_path)
        dumped = yaml.safe_load(out_path.read_text())
        step = next(t for t in dumped["tasks"] if t["id"] == "step")
        assert step["runtime"]["func"] == f"{function_module}:work"

        loaded = BaseWorkflow.from_yaml(out_path)
        await loaded.run(trigger_id="step")

        task = next(t for t in loaded.tasks if t.id == "step")
        assert (Path(task.working_dir) / "ran.txt").read_text() == "ok"
