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
Plugin skeleton for horus-runtime.

Copy this module into your plugin package, swap the base class + abstract
methods for the domain you are extending (see the horus-plugin skill's
references/plugin-catalog.md), then register the *module* under the matching
``horus.<domain>`` entry-point group in pyproject.toml.

The example below implements a trivial ``BaseArtifact`` (a text file). Delete
it and follow the same shape for BaseTarget, BaseRuntime, BaseExecutor, etc.
"""

from typing import ClassVar

from horus_runtime.core.artifact.base import BaseArtifact
from horus_runtime.i18n import tr as _


class MyArtifact(BaseArtifact[str]):
    """
    Example artifact plugin: a UTF-8 text file whose native value is ``str``.
    """

    # 1. Discriminator + registry key. Must be a non-empty default and unique
    #    within the ``horus.artifact`` domain.
    kind: str = "my_artifact"

    # 2. UI metadata (shown by the Horus GUI). ``kind_description`` is wrapped
    #    with ``tr`` so it can be translated.
    kind_name: ClassVar[str] = "My Artifact"
    kind_description: ClassVar[str] = _("A UTF-8 text file artifact.")

    # 3. Implement the domain's abstract methods.
    def read(self) -> str:
        """Read and decode the artifact contents."""
        return self.path.read_text(encoding="utf-8")

    def write(self, value: str) -> None:
        """Write the native representation to the canonical path."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(value, encoding="utf-8")
