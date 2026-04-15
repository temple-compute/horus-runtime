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

import pytest

from horus_builtin.target.local import LocalTarget
from horus_builtin.transfer.local_noop import LocalNoOpTransfer
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget
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
