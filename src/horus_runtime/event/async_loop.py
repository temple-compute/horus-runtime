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
Utility for running an asyncio event loop in a separate thread, allowing
synchronous code to submit async tasks without blocking. This is
primarily used for the HorusBus to run async transports without requiring
the entire runtime to be async.
"""

import asyncio
import threading
from collections.abc import Coroutine


class BusAsyncLoopThread:
    """
    Helper class to run an asyncio event loop in a separate thread.
    """

    def __init__(self) -> None:
        """
        Initializes the event loop and starts the thread.
        """
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        """
        Target function for the thread, runs the event loop indefinitely.
        """
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Coroutine[None, None, None]) -> None:
        """
        Submit a coroutine to be run on the event loop thread.
        """
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self) -> None:
        """
        Stop the event loop and join the thread.
        """
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
