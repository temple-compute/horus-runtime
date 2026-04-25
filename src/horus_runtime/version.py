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
This module provides the version of the Horus Runtime.
"""

from importlib.metadata import PackageNotFoundError, version


def _get_version() -> str:
    """
    Get the version of the Horus Runtime from the package metadata.
    If the package is not found (e.g. in development), return "dev".
    """
    try:
        return version("horus-runtime")
    except PackageNotFoundError:
        return "dev"


__version__: str = _get_version()
