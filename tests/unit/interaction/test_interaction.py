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
Unit tests for the interaction architecture.
"""

import inspect
from collections.abc import Generator
from typing import ClassVar

import pytest

from horus_builtin.interaction.cli import CLIInteractionTransport
from horus_builtin.interaction.common.confirm import ConfirmInteraction
from horus_builtin.interaction.common.string import StringInteraction
from horus_runtime.context import HorusContext, _runtime_ctx
from horus_runtime.core.interaction.base import BaseInteraction
from horus_runtime.core.interaction.exceptions import (
    InteractionParseError,
    RendererNotFoundError,
)
from horus_runtime.core.interaction.renderer import BaseInteractionRenderer
from horus_runtime.core.interaction.transport import (
    BaseInteractionTransport,
    InteractionAnsweredEvent,
    InteractionAskedEvent,
    InteractionFailedEvent,
    InteractionRetryEvent,
)


class ConcreteTestInteraction(BaseInteraction[str]):
    """
    Concrete interaction used to validate BaseInteraction behavior.
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "test_interaction"

    async def parse(self, value: object) -> str:
        """
        Return the provided value as a string.
        """
        return str(value)


class ConcreteTestTransport(BaseInteractionTransport):
    """
    Concrete transport used to validate BaseInteractionTransport behavior.
    """

    add_to_registry: ClassVar[bool] = False
    kind: str = "test_transport"


class ConcreteTestRenderer(
    BaseInteractionRenderer[
        ConcreteTestTransport,
        ConcreteTestInteraction,
    ]
):
    """
    Concrete renderer used to validate BaseInteractionRenderer behavior.
    """

    add_to_registry: ClassVar[bool] = False
    handles_transport: ClassVar[type[ConcreteTestTransport]] = (
        ConcreteTestTransport
    )
    handles_interaction: ClassVar[type[ConcreteTestInteraction]] = (
        ConcreteTestInteraction
    )

    async def render(
        self,
        transport: ConcreteTestTransport,
        interaction: ConcreteTestInteraction,
    ) -> object:
        """
        Return a fixed raw value for testing.
        """
        del transport
        del interaction
        return "rendered"


@pytest.fixture
def emitted_events(monkeypatch: pytest.MonkeyPatch) -> Generator[list[object]]:
    """
    Provide a HorusContext whose event bus records emitted events.
    """
    events: list[object] = []
    ctx = HorusContext()
    token = _runtime_ctx.set(ctx)

    monkeypatch.setattr(ctx.bus, "emit", events.append)

    try:
        yield events
    finally:
        _runtime_ctx.reset(token)


@pytest.mark.unit
class TestBaseInteraction:
    """
    Test cases for BaseInteraction.
    """

    def test_base_interaction_is_abstract(self) -> None:
        """
        Test that BaseInteraction cannot be instantiated directly.
        """
        with pytest.raises(TypeError):
            BaseInteraction(kind="test", batch_key="batch")  # type: ignore

    def test_registry_key_is_kind(self) -> None:
        """
        Test that BaseInteraction uses 'kind' as registry key.
        """
        assert BaseInteraction.registry_key == "kind"

    def test_parse_method_is_abstract(self) -> None:
        """
        Test that parse is marked as abstract.
        """
        assert "parse" in BaseInteraction.__abstractmethods__

    def test_parse_method_signature(self) -> None:
        """
        Test that parse has the expected method signature.
        """
        signature = inspect.signature(BaseInteraction.parse)

        params = list(signature.parameters.keys())
        assert params == ["self", "value"]

    def test_base_interaction_has_required_fields(self) -> None:
        """
        Test that BaseInteraction exposes the expected model fields.
        """
        fields = BaseInteraction.model_fields

        expected_fields = {
            "kind",
            "batch_key",
            "title",
            "prompt",
            "description",
            "default",
            "value",
        }
        assert expected_fields.issubset(fields.keys())

    async def test_concrete_interaction_preserves_defaults(self) -> None:
        """
        Test that a concrete interaction stores the configured metadata.
        """
        interaction = ConcreteTestInteraction(
            batch_key="test_batch",
            title="Title",
            prompt="Prompt",
            description="Description",
            default="fallback",
        )

        assert interaction.kind == "test_interaction"
        assert interaction.batch_key == "test_batch"
        assert interaction.title == "Title"
        assert interaction.prompt == "Prompt"
        assert interaction.description == "Description"
        assert interaction.default == "fallback"
        assert interaction.value is None
        assert await interaction.parse(123) == "123"


@pytest.mark.unit
class TestBaseInteractionRenderer:
    """
    Test cases for BaseInteractionRenderer.
    """

    def test_base_interaction_renderer_is_abstract(self) -> None:
        """
        Test that BaseInteractionRenderer cannot be instantiated directly.
        """
        with pytest.raises(TypeError):
            BaseInteractionRenderer()  # type: ignore

    def test_registry_key_is_render_key(self) -> None:
        """
        Test that BaseInteractionRenderer uses 'render_key' as registry key.
        """
        assert BaseInteractionRenderer.registry_key == "render_key"

    def test_render_method_is_abstract(self) -> None:
        """
        Test that render is marked as abstract.
        """
        assert "render" in BaseInteractionRenderer.__abstractmethods__

    def test_init_subclass_sets_render_key(self) -> None:
        """
        Test that render_key is derived from transport and interaction kinds.
        """
        assert ConcreteTestRenderer.model_fields["render_key"].default == (
            "test_transport:test_interaction"
        )

    def test_get_from_registry_returns_matching_renderer(self) -> None:
        """
        Test that get_from_registry returns the registered renderer class.
        """
        transport = CLIInteractionTransport()
        interaction = StringInteraction(batch_key="batch")

        renderer_cls = BaseInteractionRenderer.get_from_registry(
            transport, interaction
        )

        assert renderer_cls is not None
        assert renderer_cls.model_fields["render_key"].default == "cli:string"


@pytest.mark.unit
class TestBaseInteractionTransport:
    """
    Test cases for BaseInteractionTransport.
    """

    async def test_ask_returns_parsed_result_and_emits_events(
        self,
        monkeypatch: pytest.MonkeyPatch,
        emitted_events: list[object],
    ) -> None:
        """
        Test that ask() renders, parses, and emits asked/answered events.
        """
        transport = CLIInteractionTransport()
        interaction = StringInteraction(
            batch_key="batch-1",
            title="Greeting",
            prompt="Enter text",
        )

        def fake_ask_text(
            self: CLIInteractionTransport,
            **_: object,
        ) -> str:
            """
            Return a fixed answer without prompting for input.
            """
            del self
            return "  hello world  "

        monkeypatch.setattr(
            CLIInteractionTransport,
            "ask_text",
            fake_ask_text,
        )

        result = await transport.ask(interaction)

        # No magic values
        no_emitted_events = 2

        assert result == "hello world"
        assert len(emitted_events) == no_emitted_events
        assert isinstance(emitted_events[0], InteractionAskedEvent)
        assert emitted_events[0].renderer_key == "cli:string"
        assert isinstance(emitted_events[1], InteractionAnsweredEvent)
        assert emitted_events[1].batch_key == "batch-1"

    async def test_ask_retries_after_parse_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        emitted_events: list[object],
    ) -> None:
        """
        Test that ask() retries when parse() raises ValueError.
        """
        transport = CLIInteractionTransport()
        interaction = ConfirmInteraction(batch_key="batch-2")
        answers = iter(["maybe", "yes"])

        def fake_ask_text(
            self: CLIInteractionTransport,
            **_: object,
        ) -> str:
            """
            Return the next canned answer without prompting for input.
            """
            del self
            return next(answers)

        monkeypatch.setattr(
            CLIInteractionTransport,
            "ask_text",
            fake_ask_text,
        )

        result = await transport.ask(interaction, max_retries=2)

        # No magic values
        n_emitted_events = 3
        n_max_retries = 2

        assert result is True
        assert len(emitted_events) == n_emitted_events
        assert isinstance(emitted_events[0], InteractionAskedEvent)
        assert isinstance(emitted_events[1], InteractionRetryEvent)
        assert emitted_events[1].attempt == 1
        assert emitted_events[1].max_retries == n_max_retries
        assert isinstance(emitted_events[2], InteractionAnsweredEvent)

    async def test_ask_raises_when_no_renderer_is_registered(
        self,
        emitted_events: list[object],
    ) -> None:
        """
        Test that ask() raises RendererNotFoundError without a matching
        renderer.
        """
        transport = ConcreteTestTransport()
        interaction = ConcreteTestInteraction(batch_key="batch-3")

        with pytest.raises(RendererNotFoundError):
            await transport.ask(interaction)

        assert len(emitted_events) == 1
        assert isinstance(emitted_events[0], InteractionFailedEvent)
        assert emitted_events[0].transport_kind == "test_transport"
        assert emitted_events[0].interaction_kind == "test_interaction"

    async def test_ask_raises_after_all_retries_are_exhausted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        emitted_events: list[object],
    ) -> None:
        """
        Test that ask() raises InteractionParseError after exhausting all
        retries.
        """
        transport = CLIInteractionTransport()
        interaction = ConfirmInteraction(batch_key="batch-4")

        def fake_ask_text(
            self: CLIInteractionTransport,
            **_: object,
        ) -> str:
            """
            Return an invalid answer without prompting for input.
            """
            del self
            return "invalid"

        monkeypatch.setattr(
            CLIInteractionTransport,
            "ask_text",
            fake_ask_text,
        )

        with pytest.raises(InteractionParseError):
            await transport.ask(interaction, max_retries=2)

        n_emitted_events = 3
        n_max_retries = 2

        assert len(emitted_events) == n_emitted_events
        assert isinstance(emitted_events[0], InteractionAskedEvent)
        assert isinstance(emitted_events[1], InteractionRetryEvent)
        assert isinstance(emitted_events[2], InteractionFailedEvent)
        assert emitted_events[1].attempt == 1
        assert emitted_events[1].max_retries == n_max_retries
        assert emitted_events[2].reason == "All 2 retries exhausted."
