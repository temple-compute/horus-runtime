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
Unit tests for task ``secrets``: credentials exported as environment
variables on every command the task runs.
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from horus_builtin.executor.shell import ShellExecutor
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.target.local import LocalTarget
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.secrets import SecretResolutionError, iter_secret_fields


def _task(tmp_path: Path, cmd: str, secrets: dict[str, Any]) -> HorusTask:
    """A shell task on a local target with *secrets*."""
    return HorusTask.model_validate(
        {
            "id": "t",
            "name": "t",
            "executor": ShellExecutor(),
            "runtime": CommandRuntime(command=cmd),
            "target": LocalTarget(working_directory=tmp_path.as_posix()),
            "secrets": secrets,
        }
    )


@pytest.mark.unit
class TestTaskSecrets:
    """Task ``secrets`` resolve into the command environment."""

    async def test_reference_reaches_the_command_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``${secret:<ref>}`` resolves from ``HORUS_SECRET_<REF>``."""
        monkeypatch.setenv("HORUS_SECRET_HF_TOKEN", "hf_abc")
        task = _task(
            tmp_path,
            "printenv HF_TOKEN > token.txt",
            {"HF_TOKEN": "${secret:hf_token}"},
        )
        await task.run()
        assert task.status is TaskStatus.COMPLETED
        token = Path(task.working_dir, "token.txt").read_text().strip()
        assert token == "hf_abc"

    async def test_missing_secret_fails_the_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unresolvable secret fails the task before its command runs."""
        monkeypatch.delenv("HORUS_SECRET_HF_TOKEN", raising=False)
        monkeypatch.delenv("HORUS_SECRETS_FILE", raising=False)
        task = _task(tmp_path, "touch ran", {"HF_TOKEN": "${secret:hf_token}"})
        with pytest.raises(SecretResolutionError):
            await task.run()
        assert task.status is TaskStatus.FAILED
        assert not Path(task.working_dir, "ran").exists()

    async def test_secrets_do_not_outlive_the_task(
        self, tmp_path: Path
    ) -> None:
        """A later task without secrets does not inherit the earlier ones."""
        first = _task(tmp_path, "true", {"API_KEY": "literal"})
        await first.run()
        second = _task(tmp_path / "b", "printenv API_KEY > k || true", {})
        await second.run()
        assert Path(second.working_dir, "k").read_text() == ""

    def test_secret_fields_are_redactable(self, tmp_path: Path) -> None:
        """Export redaction finds the field and a reference dumps as itself."""
        task = _task(tmp_path, "true", {"HF_TOKEN": "${secret:hf_token}"})
        paths = [path for path, _ in iter_secret_fields(task)]
        assert paths == ["secrets.HF_TOKEN.value"]
        dumped = task.model_dump(mode="json")
        assert dumped["secrets"] == {
            "HF_TOKEN": {
                "value": "${secret:hf_token}",
                "name": "",
                "description": "",
            }
        }

    async def test_name_and_description_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The object form keeps its labels and resolves like the shorthand."""
        monkeypatch.setenv("HORUS_SECRET_HF_TOKEN", "hf_abc")
        entry = {
            "value": "${secret:hf_token}",
            "name": "Hugging Face",
            "description": "Gated weights",
        }
        task = _task(
            tmp_path, "printenv HF_TOKEN > token.txt", {"HF_TOKEN": entry}
        )
        assert task.model_dump(mode="json")["secrets"] == {"HF_TOKEN": entry}
        await task.run()
        token = Path(task.working_dir, "token.txt").read_text().strip()
        assert token == "hf_abc"

    @pytest.mark.parametrize("name", ["PATH", "HF TOKEN", "HORUS_X"])
    def test_bad_name_is_rejected(self, tmp_path: Path, name: str) -> None:
        """A bad env var name fails validation, naming the key."""
        with pytest.raises(ValidationError, match=f"Secret '{name}': "):
            _task(tmp_path, "true", {name: "${secret:hf_token}"})
