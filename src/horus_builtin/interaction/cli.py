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
CLI interaction transport and renderers for common interactions.
"""

from typing import ClassVar

from horus_builtin.interaction.common.confirm import ConfirmInteraction
from horus_builtin.interaction.common.dropdown import DropdownInteraction
from horus_builtin.interaction.common.file import FileInteraction
from horus_builtin.interaction.common.string import StringInteraction
from horus_runtime.core.interaction.renderer import (
    BaseInteractionRenderer,
)
from horus_runtime.core.interaction.transport import BaseInteractionTransport


class CLIInteractionTransport(BaseInteractionTransport):
    """
    Interaction transport backed by terminal I/O.
    """

    kind: str = "cli"
    add_to_registry: ClassVar[bool] = True

    def ask_text(
        self,
        *,
        title: str | None,
        prompt: str | None,
        default: str | None,
        placeholder: str | None = None,
    ) -> str:
        """
        Ask for free-form text input.
        """
        data = f"{title}\n{prompt}\n" if title or prompt else ""
        if default is not None:
            data += f"(default: {default})\n"
        if placeholder is not None:
            data += f"(placeholder: {placeholder})\n"
        data += "> "
        return input(data)


class CLIStringRenderer(
    BaseInteractionRenderer[CLIInteractionTransport, StringInteraction],
):
    """
    Render string interactions in the CLI.
    """

    handles_transport: ClassVar[type[CLIInteractionTransport]] = (
        CLIInteractionTransport
    )
    handles_interaction: ClassVar[type[StringInteraction]] = StringInteraction

    async def render(
        self,
        transport: CLIInteractionTransport,
        interaction: StringInteraction,
    ) -> object:
        """
        Ask for text through the CLI I/O adapter.
        """
        return transport.ask_text(
            title=interaction.title,
            prompt=interaction.prompt,
            default=interaction.default,
            placeholder=interaction.placeholder,
        )


class CLIConfirmRenderer(
    BaseInteractionRenderer[CLIInteractionTransport, ConfirmInteraction],
):
    """
    Render confirm interactions in the CLI.
    """

    handles_transport: ClassVar[type[CLIInteractionTransport]] = (
        CLIInteractionTransport
    )
    handles_interaction: ClassVar[type[ConfirmInteraction]] = (
        ConfirmInteraction
    )

    async def render(
        self,
        transport: CLIInteractionTransport,
        interaction: ConfirmInteraction,
    ) -> object:
        """
        Ask for confirmation through the CLI I/O adapter.
        """
        return transport.ask_text(
            title=interaction.title,
            prompt=interaction.prompt or "Confirm? (y/n)",
            default=(
                "y"
                if interaction.default
                else "n"
                if interaction.default is not None
                else None
            ),
        )


class CLIDropdownRenderer(
    BaseInteractionRenderer[CLIInteractionTransport, DropdownInteraction],
):
    """
    Render dropdown interactions in the CLI.
    """

    handles_transport: ClassVar[type[CLIInteractionTransport]] = (
        CLIInteractionTransport
    )
    handles_interaction: ClassVar[type[DropdownInteraction]] = (
        DropdownInteraction
    )

    async def render(
        self,
        transport: CLIInteractionTransport,
        interaction: DropdownInteraction,
    ) -> object:
        """
        Ask for a selection through the CLI I/O adapter.
        """
        return transport.ask_text(
            title=interaction.title,
            prompt=(
                interaction.prompt
                or f"Select one of: {', '.join(interaction.options)}"
            ),
            default=interaction.default,
        )


class CLIFileRenderer(
    BaseInteractionRenderer[CLIInteractionTransport, FileInteraction],
):
    """
    Render file interactions in the CLI.
    """

    handles_transport: ClassVar[type[CLIInteractionTransport]] = (
        CLIInteractionTransport
    )
    handles_interaction: ClassVar[type[FileInteraction]] = FileInteraction

    async def render(
        self,
        transport: CLIInteractionTransport,
        interaction: FileInteraction,
    ) -> object:
        """
        Ask for a file path through the CLI I/O adapter.
        """
        hint = (
            f" ({', '.join(interaction.accept)})" if interaction.accept else ""
        )
        return transport.ask_text(
            title=interaction.title,
            prompt=interaction.prompt or f"Enter file path{hint}:",
            default=(
                str(interaction.default)
                if interaction.default is not None
                else None
            ),
        )
