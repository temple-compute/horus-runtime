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
AutoRegistry exceptions module.
"""


class RegistryError(Exception):
    """
    Base exception for registry-related errors.
    """


class RegistryKeyAttributeNotDefinedError(RegistryError):
    """
    Exception raised when a subclass is missing a required registry key
    class variable.
    """


class RegistryKeyIsNoneError(RegistryError):
    """
    Exception raised when a subclass has a registry key class variable set
    to None or empty.
    """


class DuplicatedRegistryKeyError(RegistryError):
    """
    Exception raised when a subclass of AutoRegistry tries to register a
    discrimination key "registry_key" with an already existing key.
    """


class RegistryPointExistsError(RegistryError):
    """
    Exception raised when the provided registry_point already exists in the
    registry.
    """


class BaseRegistryClassEntryPointNotDefinedError(RegistryError):
    """
    Exception raised when the developer forgets to add "registry_point" to a
    subclass that directly inherits from AutoRegistry.
    """
