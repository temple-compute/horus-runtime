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
Shared cwd guard for the in-process Python executors.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator

# ponytail: os.chdir is process-global, so concurrently dispatched in-process
# tasks (different LocalTargets, same event loop) would clobber each other's
# cwd. This lock serializes the chdir window across both Python executors.
# Drop it only if these executors stop using chdir.
_cwd_lock = asyncio.Lock()

# TODO: Consider parallel task execution with a per-task cwd. Currently the
# lock is held for the entire duration of the task execution, which is not
# ideal.


@contextlib.asynccontextmanager
async def chdir_locked(path: os.PathLike[str] | str) -> AsyncIterator[None]:
    """
    Change the process cwd to *path* for the duration of the block, holding a
    shared lock so concurrent in-process tasks can't observe each other's cwd.
    """
    async with _cwd_lock:
        with contextlib.chdir(path):
            yield
