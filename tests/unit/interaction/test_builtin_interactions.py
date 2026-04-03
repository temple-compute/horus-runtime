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
Unit tests for built-in interactions and CLI renderers.
"""

from pathlib import Path

import pytest

from horus_builtin.artifact.file import FileArtifact
from horus_builtin.interaction.cli import (
    CLIConfirmRenderer,
    CLIDropdownRenderer,
    CLIFileRenderer,
    CLIInteractionTransport,
    CLIStringRenderer,
)
from horus_builtin.interaction.common.confirm import ConfirmInteraction
from horus_builtin.interaction.common.dropdown import DropdownInteraction
from horus_builtin.interaction.common.file import FileInteraction
from horus_builtin.interaction.common.string import StringInteraction
from horus_runtime.core.interaction.base import BaseInteraction
from horus_runtime.core.interaction.renderer import BaseInteractionRenderer
from horus_runtime.core.interaction.transport import BaseInteractionTransport


@pytest.mark.unit
class TestInitRegistry:
    """
    Test that built-in interaction components are registered.
    """

    def test_builtin_interactions_are_registered(self) -> None:
        """
        Test that built-in interaction kinds are available in the registry.
        """
        assert BaseInteraction.registry["string"] is StringInteraction
        assert BaseInteraction.registry["confirm"] is ConfirmInteraction
        assert BaseInteraction.registry["dropdown"] is DropdownInteraction
        assert BaseInteraction.registry["file"] is FileInteraction

    def test_cli_transport_is_registered(self) -> None:
        """
        Test that the CLI transport is registered.
        """
        assert BaseInteractionTransport.registry["cli"] is (
            CLIInteractionTransport
        )

    def test_cli_renderers_are_registered(self) -> None:
        """
        Test that CLI renderers are registered under the expected keys.
        """
        assert BaseInteractionRenderer.registry["cli:string"] is (
            CLIStringRenderer
        )
        assert BaseInteractionRenderer.registry["cli:confirm"] is (
            CLIConfirmRenderer
        )
        assert BaseInteractionRenderer.registry["cli:dropdown"] is (
            CLIDropdownRenderer
        )
        assert BaseInteractionRenderer.registry["cli:file"] is (
            CLIFileRenderer
        )


@pytest.mark.unit
class TestStringInteraction:
    """
    Test cases for StringInteraction.
    """

    async def test_parse_strips_whitespace_by_default(self) -> None:
        """
        Test that parse() strips surrounding whitespace by default.
        """
        interaction = StringInteraction(batch_key="batch")

        assert await interaction.parse("  hello  ") == "hello"

    async def test_parse_preserves_whitespace_when_strip_is_false(
        self,
    ) -> None:
        """
        Test that parse() preserves whitespace when strip=False.
        """
        interaction = StringInteraction(batch_key="batch", strip=False)

        assert await interaction.parse("  hello  ") == "  hello  "

    async def test_parse_uses_default_for_empty_values(self) -> None:
        """
        Test that parse() returns the default for empty input.
        """
        interaction = StringInteraction(
            batch_key="batch",
            default="fallback",
        )

        assert await interaction.parse("") == "fallback"


@pytest.mark.unit
class TestConfirmInteraction:
    """
    Test cases for ConfirmInteraction.
    """

    async def test_parse_accepts_truthy_values(self) -> None:
        """
        Test that parse() accepts common truthy inputs.
        """
        interaction = ConfirmInteraction(batch_key="batch")

        assert await interaction.parse("yes") is True
        assert await interaction.parse("1") is True

    async def test_parse_accepts_falsy_values(self) -> None:
        """
        Test that parse() accepts common falsy inputs.
        """
        interaction = ConfirmInteraction(batch_key="batch")

        assert await interaction.parse("no") is False
        assert await interaction.parse("0") is False

    async def test_parse_uses_default_for_empty_values(self) -> None:
        """
        Test that parse() returns the default for empty input.
        """
        interaction = ConfirmInteraction(batch_key="batch", default=True)

        assert await interaction.parse("") is True

    async def test_parse_rejects_invalid_values(self) -> None:
        """
        Test that parse() raises ValueError for unknown inputs.
        """
        interaction = ConfirmInteraction(batch_key="batch")

        with pytest.raises(ValueError, match="Cannot parse confirmation"):
            await interaction.parse("later")


@pytest.mark.unit
class TestDropdownInteraction:
    """
    Test cases for DropdownInteraction.
    """

    async def test_parse_accepts_value_from_options(self) -> None:
        """
        Test that parse() accepts a configured option.
        """
        interaction = DropdownInteraction(
            batch_key="batch",
            options=["red", "green", "blue"],
        )

        assert await interaction.parse("green") == "green"

    async def test_parse_uses_default_for_empty_values(self) -> None:
        """
        Test that parse() returns the default for empty input.
        """
        interaction = DropdownInteraction(
            batch_key="batch",
            options=["red", "green"],
            default="red",
        )

        assert await interaction.parse("") == "red"

    async def test_parse_rejects_value_not_in_options(self) -> None:
        """
        Test that parse() rejects selections outside the allowed options.
        """
        interaction = DropdownInteraction(
            batch_key="batch",
            options=["red", "green"],
        )

        with pytest.raises(ValueError, match="Invalid selection"):
            await interaction.parse("blue")


@pytest.mark.unit
class TestFileInteraction:
    """
    Test cases for FileInteraction.
    """

    async def test_parse_returns_file_artifact_for_existing_file(
        self,
        tmp_path: Path,
    ) -> None:
        """
        Test that parse() returns a FileArtifact for an existing path.
        """
        file_path = tmp_path / "example.txt"
        file_path.write_text("hello")
        interaction = FileInteraction(batch_key="batch")

        artifact = await interaction.parse(file_path)

        assert isinstance(artifact, FileArtifact)
        assert artifact.path == file_path

    async def test_parse_uses_default_path_for_empty_values(
        self,
        tmp_path: Path,
    ) -> None:
        """
        Test that parse() falls back to the configured default path.
        """
        file_path = tmp_path / "default.txt"
        file_path.write_text("hello")
        interaction = FileInteraction(
            batch_key="batch",
            default=FileArtifact(path=file_path),
        )

        artifact = await interaction.parse("")

        assert artifact.path == file_path

    async def test_parse_rejects_missing_files(self) -> None:
        """
        Test that parse() raises ValueError when the file does not exist.
        """
        interaction = FileInteraction(batch_key="batch")

        with pytest.raises(ValueError, match="File not found"):
            await interaction.parse("/tmp/does-not-exist.txt")

    async def test_parse_rejects_disallowed_extensions(
        self,
        tmp_path: Path,
    ) -> None:
        """
        Test that parse() enforces accepted file extensions.
        """
        file_path = tmp_path / "example.txt"
        file_path.write_text("hello")
        interaction = FileInteraction(
            batch_key="batch",
            accept=[".json"],
        )

        with pytest.raises(ValueError, match="Expected one of"):
            await interaction.parse(file_path)


@pytest.mark.unit
class TestCLIInteractionTransport:
    """
    Test cases for CLIInteractionTransport.
    """

    def test_ask_text_formats_prompt_for_input(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Test that ask_text() composes the CLI prompt before calling input().
        """
        prompts: list[str] = []
        transport = CLIInteractionTransport()

        def fake_input(prompt: str) -> str:
            """
            Record the prompt and return a fixed answer.
            """
            prompts.append(prompt)
            return "typed value"

        monkeypatch.setattr("builtins.input", fake_input)

        result = transport.ask_text(
            title="Title",
            prompt="Prompt",
            default="fallback",
            placeholder="Type here",
        )

        assert result == "typed value"
        assert prompts == [
            "Title\nPrompt\n(default: fallback)\n(placeholder: Type here)\n> "
        ]


@pytest.mark.unit
class TestCLIRenderers:
    """
    Test cases for built-in CLI renderers.
    """

    async def test_string_renderer_passes_placeholder_to_transport(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Test that CLIStringRenderer forwards string metadata to ask_text().
        """
        transport = CLIInteractionTransport()
        interaction = StringInteraction(
            batch_key="batch",
            title="Title",
            prompt="Prompt",
            default="fallback",
            placeholder="Type here",
        )
        captured: dict[str, object] = {}

        def fake_ask_text(**kwargs: object) -> str:
            """
            Capture renderer arguments and return a fixed answer.
            """
            captured.update(kwargs)
            return "value"

        monkeypatch.setattr(
            CLIInteractionTransport,
            "ask_text",
            lambda self, **kwargs: fake_ask_text(**kwargs),
        )

        result = await CLIStringRenderer().render(transport, interaction)

        assert result == "value"
        assert captured == {
            "title": "Title",
            "prompt": "Prompt",
            "default": "fallback",
            "placeholder": "Type here",
        }

    async def test_confirm_renderer_uses_default_prompt_and_boolean_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Test that CLIConfirmRenderer maps boolean defaults to y/n strings.
        """
        transport = CLIInteractionTransport()
        interaction = ConfirmInteraction(batch_key="batch", default=True)
        captured: dict[str, object] = {}

        def fake_ask_text(**kwargs: object) -> str:
            """
            Capture renderer arguments and return a fixed answer.
            """
            captured.update(kwargs)
            return "yes"

        monkeypatch.setattr(
            CLIInteractionTransport,
            "ask_text",
            lambda self, **kwargs: fake_ask_text(**kwargs),
        )

        result = await CLIConfirmRenderer().render(transport, interaction)

        assert result == "yes"
        assert captured == {
            "title": None,
            "prompt": "Confirm? (y/n)",
            "default": "y",
        }

    async def test_dropdown_renderer_builds_prompt_from_options(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Test that CLIDropdownRenderer renders the option list in the prompt.
        """
        transport = CLIInteractionTransport()
        interaction = DropdownInteraction(
            batch_key="batch",
            options=["red", "green"],
            default="green",
        )
        captured: dict[str, object] = {}

        def fake_ask_text(**kwargs: object) -> str:
            """
            Capture renderer arguments and return a fixed answer.
            """
            captured.update(kwargs)
            return "green"

        monkeypatch.setattr(
            CLIInteractionTransport,
            "ask_text",
            lambda self, **kwargs: fake_ask_text(**kwargs),
        )

        result = await CLIDropdownRenderer().render(transport, interaction)

        assert result == "green"
        assert captured == {
            "title": None,
            "prompt": "Select one of: red, green",
            "default": "green",
        }

    async def test_file_renderer_uses_extension_hint_and_path_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """
        Test that CLIFileRenderer passes file-specific prompt metadata.
        """
        transport = CLIInteractionTransport()
        file_path = tmp_path / "example.json"

        artifact = FileArtifact(path=file_path)
        interaction = FileInteraction(
            batch_key="batch",
            accept=[".json", ".yaml"],
            default=artifact,
        )
        captured: dict[str, object] = {}

        def fake_ask_text(**kwargs: object) -> str:
            """
            Capture renderer arguments and return a fixed answer.
            """
            captured.update(kwargs)
            return str(file_path)

        monkeypatch.setattr(
            CLIInteractionTransport,
            "ask_text",
            lambda self, **kwargs: fake_ask_text(**kwargs),
        )

        result = await CLIFileRenderer().render(transport, interaction)

        assert result == str(file_path)
        assert captured == {
            "title": None,
            "prompt": "Enter file path (.json, .yaml):",
            "default": str(file_path),
        }
