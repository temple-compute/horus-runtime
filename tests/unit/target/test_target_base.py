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
Unit tests for BaseTarget abstract base class.
"""

from abc import ABC

import pytest
from pydantic import BaseModel

from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.registry.auto_registry import AutoRegistry


class ConcreteTestTarget(BaseTarget):
    """
    Minimal concrete implementation of BaseTarget for testing purposes.
    """

    kind: str = "test_target"

    @property
    def location_id(self) -> str:
        """
        Return the location identifier for this target.
        """
        return "test://localhost"

    async def dispatch(self, task: BaseTask) -> None:
        """
        Simulate dispatching a task to this target.
        """
        pass

    async def wait(self) -> None:
        """
        Simulate waiting for a dispatched task to complete.
        """
        pass

    async def cancel(self) -> None:
        """
        Simulate canceling a dispatched task.
        """
        pass

    async def get_status(self) -> TaskStatus:
        """
        Simulate retrieving the status of a dispatched task.
        """
        return TaskStatus.IDLE

    def access_cost(self, _: BaseArtifact) -> float | None:
        """
        Simulate calculating the access cost for an artifact.
        """
        return 0.0


@pytest.mark.unit
class TestBaseTarget:
    """
    Tests for the BaseTarget abstract base class.
    """

    def test_base_target_is_abstract(self) -> None:
        """
        BaseTarget cannot be instantiated directly.
        """
        with pytest.raises(TypeError):
            BaseTarget()  # type: ignore

    def test_base_target_inherits_correctly(self) -> None:
        """
        BaseTarget inherits from AutoRegistry and BaseModel.
        """
        assert issubclass(BaseTarget, BaseModel)
        assert issubclass(BaseTarget, ABC)
        assert issubclass(BaseTarget, AutoRegistry)

    def test_registry_key_is_kind(self) -> None:
        """
        BaseTarget uses 'kind' as its registry discriminator.
        """
        assert BaseTarget.registry_key == "kind"

    async def test_recover_returns_false_by_default(self) -> None:
        """
        The default recover() implementation returns False without raising.
        """
        target = ConcreteTestTarget()
        result = await target.recover()
        assert result is False

    def test_concrete_target_instantiates(self) -> None:
        """
        A fully-implemented subclass can be instantiated without error.
        """
        target = ConcreteTestTarget()
        assert target.kind == "test_target"

    def test_location_id_is_string(self) -> None:
        """
        location_id must return a non-empty string.
        """
        target = ConcreteTestTarget()
        assert isinstance(target.location_id, str)
        assert target.location_id
