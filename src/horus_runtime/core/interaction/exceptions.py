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
Exceptions for the interaction system.
"""

from horus_runtime.i18n import tr as _


class BaseInteractionError(Exception):
    """
    Base exception for interaction-related errors.
    """


class RendererNotFoundError(BaseInteractionError):
    """
    Raised when no renderer matches a transport and interaction pair.
    """

    def __init__(self, transport_kind: str, interaction_kind: str) -> None:
        """
        Initialize the error with the transport and interaction kinds.
        """
        super().__init__(
            _("No renderer registered for %(transport)s:%(interaction)s")
            % {"transport": transport_kind, "interaction": interaction_kind}
        )


class InteractionTransportNotConfiguredError(BaseInteractionError):
    """
    Raised when a task needs an interaction transport but none is available.
    """

    def __init__(self) -> None:
        """
        Initialize the error when no interaction transport is configured.
        """
        super().__init__(
            _("No interaction transport configured for this task.")
        )


class InteractionParseError(BaseInteractionError):
    """
    Raised when parsing the raw interaction answer fails after all retries.
    """

    def __init__(self, interaction_kind: str, max_retries: int) -> None:
        """
        Initialize the error with the interaction kind and maximum retries.
        """
        super().__init__(
            _(
                "Failed to parse interaction '%(kind)s' after %(retries)d "
                "retries."
            )
            % {"kind": interaction_kind, "retries": max_retries}
        )


class BatchKeyError(BaseInteractionError):
    """
    Raised when a batch transport is missing a required key.
    """

    def __init__(self, key: str) -> None:
        """
        Initialize the error with the missing batch key.
        """
        super().__init__(
            _("Missing required batch key '%(key)s'.") % {"key": key}
        )


class BatchValueError(BaseInteractionError):
    """
    Raised when a batch value fails validation.
    """

    def __init__(self, key: str, reason: str) -> None:
        """
        Initialize the error with the batch key and reason for failure.
        """
        super().__init__(
            _("Batch value for key '%(key)s' is invalid: %(reason)s")
            % {"key": key, "reason": reason}
        )


class MissingInputError(BaseInteractionError):
    """
    Raised when a configuration-backed transport is missing an input value.
    """

    def __init__(self, input_name: str) -> None:
        """
        Initialize the error with the missing input name.
        """
        super().__init__(
            _("Missing required input '%(input)s'.") % {"input": input_name}
        )


class YAMLValueError(BaseInteractionError):
    """
    Raised when a YAML-provided value fails validation.
    """

    def __init__(self, key: str, reason: str) -> None:
        """
        Initialize the error with the YAML key and reason for failure.
        """
        super().__init__(
            _("YAML value for key '%(key)s' is invalid: %(reason)s")
            % {"key": key, "reason": reason}
        )
