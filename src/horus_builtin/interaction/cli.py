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
from horus_runtime.i18n import tr as _


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
        lines = []
        if title is not None:
            lines.append(title)
        if prompt is not None:
            lines.append(prompt)
        if default is not None:
            lines.append(_("(default: %(default)s)") % {"default": default})
        if placeholder is not None:
            lines.append(
                _("(placeholder: %(placeholder)s)")
                % {"placeholder": placeholder}
            )
        lines.append("> ")
        data = "\n".join(lines)
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
            prompt=interaction.prompt or _("Confirm? (y/n)"),
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
                or _("Select one of: %(options)s")
                % {"options": ", ".join(interaction.options)}
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
            _(" (%(extensions)s)")
            % {"extensions": ", ".join(interaction.accept)}
            if interaction.accept
            else ""
        )
        return transport.ask_text(
            title=interaction.title,
            prompt=interaction.prompt
            or _("Enter file path%(hint)s:") % {"hint": hint},
            default=(
                str(interaction.default.path)
                if interaction.default is not None
                else None
            ),
        )
