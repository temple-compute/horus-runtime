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
def timed() -> Generator[Callable[[], float], None, None]:
    """
    Context manager for timing a block of code. Yields a function that returns
    the elapsed time in seconds when called.

    Usage:
    with timed() as get_elapsed:
        # some code to time
    elapsed_time = get_elapsed()
    """
    start = time.perf_counter()
    yield lambda: time.perf_counter() - start
