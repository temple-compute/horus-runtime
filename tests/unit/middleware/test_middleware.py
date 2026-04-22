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
Basic tests for the middleware system.
"""

from collections.abc import Callable, Generator
from typing import ClassVar

import pytest

from horus_runtime.context import HorusContext
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.interaction.base import BaseInteraction
from horus_runtime.core.interaction.renderer import BaseInteractionRenderer
from horus_runtime.core.interaction.transport import BaseInteractionTransport
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.task.base import BaseTask
from horus_runtime.core.task.status import TaskStatus
from horus_runtime.middleware.auto_middleware import AutoMiddleware
from horus_runtime.middleware.interaction import (
    InteractionMiddleware,
    InteractionMiddlewareContext,
)
from horus_runtime.middleware.target import (
    TargetMiddleware,
    TargetMiddlewareContext,
)
from tests.conftest import MakeTaskType


class CallMiddlewareRoot(AutoMiddleware[list[str]], entry_point="test_call"):
    """
    Test-only middleware root for validating middleware chaining.
    """

    registry: list[type["CallMiddlewareRoot"]]


@pytest.fixture
def restore_target_middleware_registry() -> Generator[None]:
    """
    Restore the target middleware registry after each test.
    """
    original_registry = list(TargetMiddleware.registry)
    try:
        yield
    finally:
        TargetMiddleware.registry = original_registry


@pytest.fixture
def restore_interaction_middleware_registry() -> Generator[None]:
    """
    Restore the interaction middleware registry after each test.
    """
    original_registry = list(InteractionMiddleware.registry)
    try:
        yield
    finally:
        InteractionMiddleware.registry = original_registry


@pytest.fixture
def restore_test_call_registry() -> Generator[None]:
    """
    Restore the test middleware registry after each test.
    """
    original_registry = list(CallMiddlewareRoot.registry)
    try:
        yield
    finally:
        CallMiddlewareRoot.registry = original_registry


class TrackingTarget(BaseTarget):
    """
    Minimal target used to verify target middleware hooks.
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "tracking_target"
    _task: BaseTask | None = None

    @property
    def location_id(self) -> str:
        """
        Return the location identifier for this target.
        """
        return "tracking://localhost"

    async def _dispatch(self, task: BaseTask) -> None:
        """
        Track the dispatched task.
        """
        self._task = task

    async def wait(self) -> None:
        """
        No-op wait implementation for middleware tests.
        """

    async def cancel(self) -> None:
        """
        No-op cancel implementation for middleware tests.
        """

    async def get_status(self) -> TaskStatus:
        """
        Return a fixed status for middleware tests.
        """
        return TaskStatus.COMPLETED

    def access_cost(self, _: BaseArtifact) -> float | None:
        """
        Return a zero-cost access estimate.
        """
        return 0.0


class DummyInteraction(BaseInteraction[str]):
    """
    Interaction used to verify middleware can mutate render context.
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "middleware_test_interaction"

    async def parse(self, value: object) -> str:
        """
        Return the raw value as a string.
        """
        return str(value)


class DummyTransport(BaseInteractionTransport):
    """
    Interaction transport used to verify middleware mutations.
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "middleware_test_transport"
    label: str = "original"


class MutatedTransport(DummyTransport):
    """
    Replacement transport injected by middleware.
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "middleware_test_transport_mutated"
    label: str = "mutated"


class DummyRenderer(BaseInteractionRenderer[DummyTransport, DummyInteraction]):
    """
    Renderer used to verify middleware mutations.
    """

    handles_transport: ClassVar[type[DummyTransport]] = DummyTransport
    handles_interaction: ClassVar[type[DummyInteraction]] = DummyInteraction

    async def render(
        self,
        transport: DummyTransport,
        interaction: DummyInteraction,
    ) -> object:
        """
        Return the current transport label.
        """
        del interaction
        return transport.label


@pytest.mark.unit
class TestMiddleware:
    """
    Basic middleware behavior tests.
    """

    async def test_call_with_middleware_wraps_in_registration_order(
        self,
        restore_test_call_registry: None,
    ) -> None:
        """
        Middleware wraps the call in registration order.
        """
        del restore_test_call_registry
        events: list[str] = []

        class OuterMiddleware(CallMiddlewareRoot):
            """
            Outer middleware used to verify wrapper ordering.
            """

            async def before(self, context: list[str]) -> None:
                """
                Record outer entry.
                """
                context.append("outer_before")

            async def after(self, context: list[str]) -> None:
                """
                Record outer exit.
                """
                context.append("outer_after")

        class InnerMiddleware(CallMiddlewareRoot):
            """
            Inner middleware used to verify wrapper ordering.
            """

            async def before(self, context: list[str]) -> None:
                """
                Record inner entry.
                """
                context.append("inner_before")

            async def after(self, context: list[str]) -> None:
                """
                Record inner exit.
                """
                context.append("inner_after")

        async def _append_and_return(
            events: list[str],
            event: str,
            result: str,
        ) -> str:
            """
            Append an event and return a fixed result.
            """
            events.append(event)
            return result

        result = await CallMiddlewareRoot.call_with_middleware(
            events,
            lambda: _append_and_return(events, "call", "done"),
        )

        assert result == "done"
        assert events == [
            "outer_before",
            "inner_before",
            "call",
            "inner_after",
            "outer_after",
        ]

    async def test_target_middleware_wraps_target_operations(
        self,
        restore_target_middleware_registry: None,
        make_shell_task: MakeTaskType,
    ) -> None:
        """
        Target middleware runs around dispatch, wait, cancel, and status calls.
        """
        del restore_target_middleware_registry
        events: list[str] = []

        class RecordingTargetMiddleware(TargetMiddleware):
            """
            Record target middleware entry and exit events.
            """

            async def before(self, context: TargetMiddlewareContext) -> None:
                """
                Record target middleware entry.
                """
                task_id = context.task.id if context.task else "none"
                events.append(f"before:{task_id}")

            async def after(self, context: TargetMiddlewareContext) -> None:
                """
                Record target middleware exit.
                """
                task_id = context.task.id if context.task else "none"
                events.append(f"after:{task_id}")

        target = TrackingTarget()
        task = make_shell_task()
        task.target = target

        await target.dispatch(task)

        assert events == [
            f"before:{task.id}",
            f"after:{task.id}",
        ]

    async def test_interaction_middleware_can_mutate_render_transport(
        self,
        horus_context: HorusContext,
        restore_interaction_middleware_registry: Callable[[], Generator[None]],
    ) -> None:
        """
        Interaction middleware mutations affect the renderer call.
        """
        del horus_context
        del restore_interaction_middleware_registry

        class SwapTransportMiddleware(InteractionMiddleware):
            """
            Replace the transport in the interaction middleware context.
            """

            async def before(
                self,
                context: InteractionMiddlewareContext,
            ) -> None:
                """
                Swap the current transport for a mutated one.
                """
                context.transport = MutatedTransport()

        transport = DummyTransport()
        interaction = DummyInteraction(value_key="value")

        result = await transport.ask(interaction)

        assert result == "mutated"
