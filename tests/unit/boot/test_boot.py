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
Test module for horus-runtime boot process.
"""

import contextvars

import pytest

from horus_runtime.context import HorusContext


@pytest.mark.unit
class TestBoot:
    """
    Test cases for HorusContext boot process.
    """

    def test_boot_sets_context(self) -> None:
        """
        Test that boot sets a HorusContext instance in the context.
        """
        ctx = contextvars.copy_context()
        ctx.run(HorusContext.boot)

        runtime = ctx.run(HorusContext.get_context)
        assert isinstance(runtime, HorusContext)

    def test_get_context_raises_before_boot(self) -> None:
        """
        Test that get_context raises RuntimeError when boot has not been
        called.
        """
        ctx = contextvars.copy_context()
        with pytest.raises(RuntimeError):
            ctx.run(HorusContext.get_context)
