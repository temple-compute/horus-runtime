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

from horus_runtime.core.target.base import BaseTarget
from horus_runtime.i18n import tr as _


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
            _(
                "No transfer strategy registered for '%(source_kind)s' → "
                "'%(destination_kind)s'. Register a BaseTransferStrategy "
                "subclass that declares handles_source and "
                "handles_destination for these target kinds."
            )
            % {
                "source_kind": source_kind,
                "destination_kind": destination_kind,
            }
        )


class OrchestratorTargetNotSetError(TransferError):
    """
    Raised when a root input artifact (one not produced by any upstream task)
    cannot be accessed by the destination target and no orchestrator_target
    has been configured on the workflow to act as the transfer source.
    """

    def __init__(
        self, artifact_id: str, destination_target: BaseTarget
    ) -> None:
        super().__init__(
            _(
                "Artifact '%(artifact_id)s' is not accessible by target "
                "'%(destination_target_kind)s' and no orchestrator_target is "
                "set on the workflow. Set workflow.orchestrator_target to the "
                "target that holds user-provided root artifacts."
            )
            % {
                "artifact_id": artifact_id,
                "destination_target_kind": destination_target.kind,
            }
        )
