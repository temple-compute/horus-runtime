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
Target command middleware system for the horus-runtime.

Wraps :meth:`BaseTarget.run_command` so plugins can inspect or rewrite the
command string before it is launched on the target host. The context is
mutable: middleware set ``ctx.command`` in place and the rewritten value is
what actually runs (locally or over a remote channel).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from horus_runtime.middleware.auto_middleware import AutoMiddleware

if TYPE_CHECKING:
    from horus_runtime.core.target.base import BaseTarget


@dataclass
class TargetCommandMiddlewareContext:
    """
    Context passed to TargetCommandMiddleware.

    ``command`` is mutable: middleware may rewrite it in place (e.g. to wrap
    the command with an instrumentation tool) before it is dispatched.
    """

    target: "BaseTarget"
    command: str
    cwd: str | None
    env: dict[str, str] | None
    detach: bool | None


class TargetCommandMiddleware(
    AutoMiddleware[TargetCommandMiddlewareContext],
    entry_point="target_command",
):
    """
    Base class for target command middleware.
    """

    registry: list[type["TargetCommandMiddleware"]]
