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
Tests for the ``Secret`` field marker, its enumeration, and redaction.
"""

import zipfile
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from horus_builtin.target.local import LocalTarget
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.packaging import package_workflow
from horus_runtime.secrets import (
    Secret,
    SecretResolutionError,
    env_key_for_ref,
    iter_secret_fields,
    iter_secret_refs,
    redact,
)

HUNTER2 = "hunter2"  # test fixture value, not a real secret


class SecretTarget(LocalTarget):
    """A LocalTarget with one secret-marked field, for testing the walk."""

    kind: str = "secret_target"
    password: Secret | None = None


class Nested(BaseModel):
    """A plain nested model with its own secret field."""

    inner: Secret | None = None


class Holder(BaseModel):
    """Exercises every shape :func:`iter_secret_fields` walks."""

    name: str
    secret: Secret | None = None
    nested: Nested | None = None
    many: list[Nested] = []


WORKFLOW_YAML = f"""
kind: horus_workflow
name: Has A Secret
tasks:
  - kind: horus_task
    id: run
    name: Run
    executor:
      kind: shell
    runtime:
      kind: command
      command: echo hi
    target:
      kind: secret_target
      password: {HUNTER2}
"""


@pytest.fixture
def workflow_dir(tmp_path: Path) -> Path:
    """A workflow directory holding one workflow with a literal password."""
    (tmp_path / "workflow.yaml").write_text(WORKFLOW_YAML)
    return tmp_path


@pytest.mark.unit
class TestSecret:
    """Tests for the ``Secret`` type itself."""

    def test_literal_value_has_no_ref(self) -> None:
        """A plain string is a literal secret: no reference to resolve."""
        secret = Secret(HUNTER2)
        assert secret.ref is None
        assert secret.get_secret_value() == HUNTER2

    def test_masks_repr_like_secretstr(self) -> None:
        """repr/str never leak the value, inherited from SecretStr."""
        assert HUNTER2 not in repr(Secret(HUNTER2))
        assert str(Secret(HUNTER2)) == "**********"

    def test_reference_resolves_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``${secret:<ref>}`` resolves from ``HORUS_SECRET_<ref>``."""
        monkeypatch.setenv("HORUS_SECRET_MY_REF", "resolved-value")
        secret = Secret("${secret:my-ref}")
        assert secret.ref == "my-ref"
        assert secret.resolve() == "resolved-value"

    def test_reference_resolves_from_secrets_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls back to ``$HORUS_SECRETS_FILE`` when no env var is set."""
        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text("my-ref: from-file\n")
        monkeypatch.delenv("HORUS_SECRET_MY_REF", raising=False)
        monkeypatch.setenv("HORUS_SECRETS_FILE", str(secrets_file))
        assert Secret("${secret:my-ref}").resolve() == "from-file"

    def test_unresolved_reference_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither source having the ref is a hard failure, not a blank."""
        monkeypatch.delenv("HORUS_SECRET_MISSING", raising=False)
        monkeypatch.delenv("HORUS_SECRETS_FILE", raising=False)
        with pytest.raises(SecretResolutionError, match="missing"):
            Secret("${secret:missing}").resolve()

    def test_default_json_dump_masks_a_reference_without_resolving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """model_dump never resolves; masking a reference must not raise."""
        monkeypatch.delenv("HORUS_SECRET_MY_REF", raising=False)
        monkeypatch.delenv("HORUS_SECRETS_FILE", raising=False)
        holder = Holder(name="x", secret=Secret("${secret:my-ref}"))
        assert holder.model_dump(mode="json")["secret"] == "**********"


@pytest.mark.unit
class TestIterSecretFields:
    """Tests for the model-tree walk."""

    def test_finds_top_level_and_nested_fields(self) -> None:
        """A secret is found at every depth: top-level, nested, in a list."""
        holder = Holder(
            name="x",
            secret=Secret(HUNTER2),
            nested=Nested(inner=Secret("inner-value")),
            many=[Nested(inner=Secret("list-value"))],
        )
        found = dict(iter_secret_fields(holder))
        assert set(found) == {"secret", "nested.inner", "many.0.inner"}
        assert found["secret"].resolve() == HUNTER2

    def test_unset_secret_field_yields_nothing(self) -> None:
        """An unset ``Secret | None`` field is not a secret to redact."""
        assert list(iter_secret_fields(Holder(name="x"))) == []

    def test_walks_a_workflow_to_a_target_field(
        self, workflow_dir: Path
    ) -> None:
        """The walk reaches a secret nested inside a task's target."""
        workflow = BaseWorkflow.from_yaml(workflow_dir / "workflow.yaml")
        found = dict(iter_secret_fields(workflow))
        assert "tasks.0.target.password" in found
        assert found["tasks.0.target.password"].resolve() == HUNTER2


@pytest.mark.unit
@pytest.mark.usefixtures("horus_context")
class TestIterSecretRefs:
    """Tests for the dict-side walk tc-os runs on stored ``workflow_data``."""

    def test_finds_refs_in_a_redacted_yaml_dict(
        self, workflow_dir: Path
    ) -> None:
        """to_yaml -> safe_load -> iter_secret_refs finds the same path."""
        workflow = BaseWorkflow.from_yaml(workflow_dir / "workflow.yaml")
        out = workflow_dir / "out.yaml"
        workflow.to_yaml(out)

        data = yaml.safe_load(out.read_text())
        found = dict(iter_secret_refs(data))
        assert found["tasks.0.target.password"] == "tasks_0_target_password"
        assert (
            env_key_for_ref(found["tasks.0.target.password"])
            == "HORUS_SECRET_TASKS_0_TARGET_PASSWORD"
        )

    def test_no_references_yields_nothing(self) -> None:
        """A plain value is not a reference."""
        assert list(iter_secret_refs({"name": "x", "port": 22})) == []


@pytest.mark.unit
class TestRedact:
    """Tests for turning secret fields into ``${secret:<ref>}`` references."""

    def test_replaces_literal_with_a_path_derived_ref(self) -> None:
        """A literal with no ref of its own gets one derived from its path."""
        holder = Holder(name="x", secret=Secret(HUNTER2))
        data = holder.model_dump(mode="json")
        redacted = redact(data, iter_secret_fields(holder))
        assert redacted["secret"] == "${secret:secret}"
        assert HUNTER2 not in str(redacted)
        # The dict redact() was given is untouched (a deep copy is redacted).
        assert data["secret"] == "**********"

    def test_keeps_an_existing_reference(self) -> None:
        """Redacting an already-referenced value is a no-op on its ref."""
        holder = Holder(name="x", secret=Secret("${secret:my-ref}"))
        data = holder.model_dump(mode="json")
        redacted = redact(data, iter_secret_fields(holder))
        assert redacted["secret"] == "${secret:my-ref}"


@pytest.mark.unit
@pytest.mark.usefixtures("horus_context")
class TestWorkflowExport:
    """Tests that export paths never write the literal secret value."""

    def test_to_yaml_never_writes_the_literal(
        self, workflow_dir: Path
    ) -> None:
        """to_yaml redacts, and the reference round-trips on reload."""
        workflow = BaseWorkflow.from_yaml(workflow_dir / "workflow.yaml")
        out = workflow_dir / "out.yaml"
        workflow.to_yaml(out)

        text = out.read_text()
        assert HUNTER2 not in text
        assert "${secret:tasks_0_target_password}" in text

        reimported = BaseWorkflow.from_yaml(out)
        target = reimported.tasks[0].target
        assert isinstance(target, SecretTarget)
        assert target.password is not None
        assert target.password.ref == "tasks_0_target_password"

    def test_package_workflow_never_writes_the_literal(
        self, workflow_dir: Path
    ) -> None:
        """The zipped workflow.yaml is redacted, byte for byte."""
        archive, _members, _skipped = package_workflow(
            workflow_dir / "workflow.yaml"
        )
        with zipfile.ZipFile(archive) as bundle:
            text = bundle.read("workflow.yaml").decode()

        assert HUNTER2 not in text
        assert "${secret:tasks_0_target_password}" in text
        # Nothing else about the file changed shape: it still parses as the
        # same workflow, minus the redacted value.
        assert "kind: horus_task" in text
