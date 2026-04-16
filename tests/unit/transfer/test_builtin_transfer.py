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
Unit tests for built-in transfer strategies.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from horus_builtin.target.local import LocalTarget
from horus_builtin.transfer.local_noop import LocalNoOpTransfer
from horus_runtime.core.transfer.strategy import BaseTransferStrategy


@pytest.mark.unit
class TestLocalNoOpTransfer:
    """
    Tests for the LocalNoOpTransfer strategy.
    """

    def test_transfer_key_is_local_local(self) -> None:
        """
        LocalNoOpTransfer derives the transfer key 'local.local' from the
        'kind' defaults of its source and destination target types.
        """
        assert LocalNoOpTransfer().transfer_key == "local.local"

    def test_handles_source_is_local_target(self) -> None:
        """
        LocalNoOpTransfer declares LocalTarget as its source type.
        """
        assert LocalNoOpTransfer.handles_source is LocalTarget

    def test_handles_destination_is_local_target(self) -> None:
        """
        LocalNoOpTransfer declares LocalTarget as its destination type.
        """
        assert LocalNoOpTransfer.handles_destination is LocalTarget

    def test_is_registered_in_transfer_registry(self) -> None:
        """
        LocalNoOpTransfer is reachable from the BaseTransferStrategy registry
        under its derived key.
        """
        assert (
            BaseTransferStrategy.registry.get("local.local")
            is LocalNoOpTransfer
        )

    def test_get_from_registry_resolves_to_local_noop(self) -> None:
        """
        get_from_registry returns LocalNoOpTransfer when both source and
        destination are LocalTarget instances.
        """
        source = LocalTarget()
        destination = LocalTarget()
        result = BaseTransferStrategy.get_from_registry(source, destination)
        assert result is LocalNoOpTransfer

    async def test_transfer_completes_without_error(self) -> None:
        """
        transfer() runs to completion without raising for any combination
        of artifact, source, and destination.
        """
        strategy = LocalNoOpTransfer()
        artifact = MagicMock()
        source = LocalTarget()
        destination = LocalTarget()

        await strategy.transfer(artifact, source, destination)

    async def test_transfer_does_not_interact_with_artifact(self) -> None:
        """
        The no-op transfer must not read, write, or modify the artifact.
        """
        strategy = LocalNoOpTransfer()
        artifact = MagicMock()
        artifact.read = AsyncMock()
        artifact.write = AsyncMock()
        source = LocalTarget()
        destination = LocalTarget()

        await strategy.transfer(artifact, source, destination)

        artifact.read.assert_not_called()
        artifact.write.assert_not_called()
