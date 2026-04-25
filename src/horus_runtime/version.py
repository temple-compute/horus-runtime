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
