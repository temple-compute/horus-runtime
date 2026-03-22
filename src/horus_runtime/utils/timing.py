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
Timing utilities for horus-runtime.
"""

import time
from collections.abc import Callable, Generator
from contextlib import contextmanager


@contextmanager
def timed() -> Generator[Callable[[], float]]:
    """
    Context manager for timing a block of code.

    Yields a function that returns the elapsed time in seconds when called.
    You can call it multiple times mid-block to see running time.

    After the block ends, the elapsed time is frozen.

    Usage:
    ```python
    with timed() as get_time:
        time.sleep(0.5)
        print(get_time()) # ~0.5s
        time.sleep(0.3)
        print(get_time()) # ~0.8s

    print(get_time()) # ~0.8s, frozen after block ends
    ```
    """
    start = time.perf_counter()
    elapsed: float | None = None

    def get_elapsed() -> float:
        return elapsed if elapsed is not None else time.perf_counter() - start

    try:
        yield get_elapsed
    finally:
        elapsed = time.perf_counter() - start
