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
Exceptions for artifact transfer in the Horus runtime.
"""


class TransferError(Exception):
    """
    Base exception for artifact transfer errors.
    """


class TransferStrategyNotFoundError(TransferError):
    """
    Raised when no registered transfer strategy can handle the given
    source → destination target pair.
    """

    def __init__(self, source_kind: str, destination_kind: str) -> None:
        super().__init__(
            f"No transfer strategy registered for '{source_kind}' → "
            f"'{destination_kind}'. Register a BaseTransferStrategy subclass "
            f"that declares handles_source and handles_destination for these "
            f"target kinds."
        )


class OrchestratorTargetNotSetError(TransferError):
    """
    Raised when a root input artifact (one not produced by any upstream task)
    cannot be accessed by the destination target and no orchestrator_target
    has been configured on the workflow to act as the transfer source.
    """

    def __init__(self, artifact_uri: str, destination_kind: str) -> None:
        super().__init__(
            f"Artifact '{artifact_uri}' is not accessible by target "
            f"'{destination_kind}' and no orchestrator_target is set on the "
            f"workflow. Set workflow.orchestrator_target to the target that "
            f"holds user-provided root artifacts."
        )
