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
This module provides functionality to scan a package for modules that subclass
the AutoRegistry class.
"""

import importlib
import pkgutil
from types import ModuleType


def scan_package(package: ModuleType):
    """
    Dynamically import all modules in a package to trigger AutoRegistry.
    """

    for _, name, _ in pkgutil.iter_modules(package.__path__):
        full_name = f"{package.__name__}.{name}"
        importlib.import_module(full_name)
