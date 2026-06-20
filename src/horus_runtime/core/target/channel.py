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
Channel primitives for agentless target communication.
"""

from abc import ABC, abstractmethod


class ChannelProcess(ABC):
    """
    Abstract handle for a command running on a target channel.

    Returned by :meth:`~horus_runtime.core.target.base.BaseTarget.run_command`.
    All I/O is **bytes**; callers are responsible for decoding.
    """

    @property
    @abstractmethod
    def returncode(self) -> int | None:
        """
        Exit code of the process, or ``None`` if it has not yet terminated.
        """

    @abstractmethod
    async def wait(self) -> int:
        """
        Wait for the process to finish and return its exit code.

        Returns:
            The integer exit code.
        """

    @abstractmethod
    async def communicate(self) -> tuple[bytes, bytes]:
        """
        Wait for the process to finish, then return ``(stdout, stderr)`` as
        raw bytes. Callers decode as needed.

        Returns:
            A ``(stdout, stderr)`` tuple, both as :class:`bytes`.
        """

    @abstractmethod
    def kill(self) -> None:
        """
        Kill the process.
        """

    @abstractmethod
    def signal(self, sig: int) -> None:
        """
        Send *sig* to the process.

        Args:
            sig: A signal number (e.g. ``signal.SIGTERM``).
        """
