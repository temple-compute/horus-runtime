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
No-op transfer strategy for local-to-local artifact transfers.
"""

from horus_builtin.target.local import LocalTarget
from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.core.target.base import BaseTarget
from horus_runtime.core.transfer.strategy import BaseTransferStrategy


class LocalNoOpTransfer(BaseTransferStrategy):
    """
    When both producer and consumer run on the same local machine,
    no transfer is needed, the artifact is already accessible.
    """

    handles_source = LocalTarget
    handles_destination = LocalTarget

    async def transfer(
        self,
        artifact: BaseArtifact,
        source: BaseTarget,
        destination: BaseTarget,
    ) -> None:
        """
        No-op: artifact is already on the local filesystem.
        """
