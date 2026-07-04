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
Exceptions related to task targets in the Horus runtime.
"""

from horus_runtime.i18n import tr as _


class BaseTargetError(Exception):
    """
    Base exception for target-related errors in the Horus runtime.
    """


class WorkingDirectoryNotSetError(BaseTargetError):
    """
    Raised when a target's working directory is required but was never set.

    ``BaseTarget.working_directory`` defaults to ``None``. The workflow fills
    it in for targets co-located with the orchestrator; every other target
    must either be given an explicit ``working_directory`` or override
    :attr:`BaseTarget.resolved_working_directory` to derive one.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(
            _(
                "Target '%(kind)s' has no working_directory set. Provide one "
                "explicitly or use a target that derives it automatically."
            )
            % {"kind": kind}
        )
        self.kind = kind
