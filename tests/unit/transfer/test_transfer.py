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
Unit tests for BaseTransferStrategy abstract base class.
"""

from pathlib import Path

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.target.local import LocalTarget
from horus_builtin.transfer.local_noop import LocalNoOpTransfer
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.target.channel import (
    ChannelProcess,
    JobHandle,
    RemoteDirEntry,
)
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.core.transfer.strategy import BaseTransferStrategy
from horus_runtime.registry.auto_registry import AutoRegistry
from horus_runtime.registry.auto_registry_product import AutoRegistryProduct


class _UnregisteredTarget(BaseTarget):
    """
    Minimal concrete target with no registered transfer strategy,
    used to exercise the not-found path in get_from_registry.
    """

    kind: str = "_test_unreg_target"

    @property
    def location_id(self) -> str:
        return "test://unreg"

    async def _dispatch(self, task: BaseTask) -> None:
        pass

    async def wait(self) -> None:
        pass

    async def cancel(self) -> None:
        pass

    async def get_status(self) -> TaskStatus:
        return TaskStatus.IDLE

    def access_cost(self, artifact: BaseArtifact) -> float | None:
        del artifact
        return 0.0

    async def run_command_sync(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ChannelProcess:
        """
        Not used in transfer tests.
        """
        raise NotImplementedError

    async def launch(
        self,
        cmd: str,
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        job_dir: str,
    ) -> JobHandle:
        """
        Not used in transfer tests.
        """
        raise NotImplementedError

    async def poll(self, handle: JobHandle) -> int | None:
        """
        Not used in transfer tests.
        """
        raise NotImplementedError

    async def read_output(self, handle: JobHandle) -> tuple[bytes, bytes]:
        """
        Not used in transfer tests.
        """
        raise NotImplementedError

    async def send_signal(self, handle: JobHandle, sig: int) -> None:
        """
        Not used in transfer tests.
        """
        raise NotImplementedError

    async def put_file(
        self,
        content: bytes | Path,
        remote_path: str,
    ) -> None:
        """
        Not used in transfer tests.
        """

    async def get_file(self, _remote_path: str) -> bytes:
        """
        Not used in transfer tests.
        """
        return b""

    async def mkdir(self, path: str) -> None:
        """
        Not used in transfer tests.
        """

    async def list_dir(self, _path: str) -> list[RemoteDirEntry]:
        """
        Not used in transfer tests.
        """
        return []


@pytest.mark.unit
class TestBaseTransferStrategy:
    """
    Tests for the BaseTransferStrategy abstract base class.
    """

    def test_is_abstract(self) -> None:
        """
        BaseTransferStrategy cannot be instantiated directly.
        """
        with pytest.raises(TypeError):
            BaseTransferStrategy()  # type: ignore[abstract]

    def test_inherits_from_auto_registry(self) -> None:
        """
        BaseTransferStrategy is an AutoRegistry subclass.
        """
        assert issubclass(BaseTransferStrategy, AutoRegistry)

    def test_inherits_from_auto_registry_product(self) -> None:
        """
        BaseTransferStrategy is an AutoRegistryProduct subclass.
        """
        assert issubclass(BaseTransferStrategy, AutoRegistryProduct)

    def test_registry_key_normalized_to_field_name(self) -> None:
        """
        After AutoRegistryProduct normalisation, registry_key is the plain
        field name 'transfer_key', not the raw composite template.
        """
        assert BaseTransferStrategy.registry_key == "transfer_key"

    def test_transfer_key_default_is_none(self) -> None:
        """
        The base class leaves transfer_key as None; concrete subclasses have
        it derived automatically.
        """
        assert (
            BaseTransferStrategy.model_fields["transfer_key"].default is None
        )

    def test_registry_is_a_dict(self) -> None:
        """
        BaseTransferStrategy exposes a registry dict for concrete strategies.
        """
        assert isinstance(BaseTransferStrategy.registry, dict)

    def test_concrete_subclass_is_registered(self) -> None:
        """
        LocalNoOpTransfer appears in the registry under 'local.local'.
        """
        assert "local.local" in BaseTransferStrategy.registry
        assert (
            BaseTransferStrategy.registry["local.local"] is LocalNoOpTransfer
        )

    def test_get_from_registry_returns_matched_strategy(self) -> None:
        """
        get_from_registry resolves the correct strategy for two LocalTarget
        instances.
        """
        source = LocalTarget()
        destination = LocalTarget()
        result = BaseTransferStrategy.get_from_registry(source, destination)
        assert result is LocalNoOpTransfer

    def test_get_from_registry_returns_none_for_unknown_combination(
        self,
    ) -> None:
        """
        get_from_registry returns None when no strategy has been registered
        for the given (source, destination) pair.
        """
        source = _UnregisteredTarget()
        destination = _UnregisteredTarget()
        result = BaseTransferStrategy.get_from_registry(source, destination)
        assert result is None


class _OtherUnregisteredTarget(_UnregisteredTarget):
    """A second target reporting a distinct ``location_id``."""

    kind: str = "_test_other_target"
    add_to_registry = False

    @property
    def location_id(self) -> str:
        return "test://other"


class _ExplodingTransfer(BaseTransferStrategy):
    """A strategy whose ``_transfer`` must never run for same locations."""

    add_to_registry = False

    async def _transfer(
        self,
        artifact: BaseArtifact,
        source: BaseTarget,
        destination: BaseTarget,
    ) -> None:
        """Fail loudly if ever reached."""
        del artifact, source, destination
        raise AssertionError("_transfer ran for a same-filesystem pair")


@pytest.mark.unit
class TestSameFilesystemShortCircuit:
    """
    The same-filesystem shortcut lives in ``transfer()``, so it applies to
    every strategy uniformly and no strategy has to implement it.
    """

    async def test_same_location_skips_strategy_and_repoints(self) -> None:
        """
        Equal ``location_id``: ``_transfer`` is never called and the artifact
        is repointed at its destination ``path_on_target``.
        """
        source = _UnregisteredTarget()
        destination = _UnregisteredTarget()
        assert source.location_id == destination.location_id
        artifact = FileArtifact(id="a", path=Path("/data/a.txt"))

        await _ExplodingTransfer().transfer(artifact, source, destination)

        assert artifact.path == Path(destination.path_on_target(artifact))

    async def test_distinct_location_runs_strategy(self) -> None:
        """
        Distinct ``location_id``: the shortcut does not fire, so the strategy's
        ``_transfer`` actually runs.
        """
        source = _UnregisteredTarget()
        destination = _OtherUnregisteredTarget()
        assert source.location_id != destination.location_id
        artifact = FileArtifact(id="a", path=Path("/data/a.txt"))

        with pytest.raises(AssertionError, match="same-filesystem"):
            await _ExplodingTransfer().transfer(artifact, source, destination)
