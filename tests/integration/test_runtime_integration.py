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
# along with this program.  If not, see <https://www.gnu.org/licenses/>.#
"""
Integration tests for horus-runtime
"""

import os
import subprocess
import sys

import pytest


@pytest.mark.integration
class TestHorusRuntimeIntegration:
    """Integration test cases"""

    def test_main_script_execution(self):
        """Test that main script can be executed"""
        src_path = os.path.join(os.path.dirname(__file__), "..", "..", "src")
        main_path = os.path.join(src_path, "main.py")

        result = subprocess.run(
            [sys.executable, main_path],
            capture_output=True,
            text=True,
            cwd=src_path,
            check=False,
        )

        assert result.returncode == 0
        assert "Horus Runtime is starting..." in result.stdout

    @pytest.mark.slow
    def test_system_integration(self):
        """Test basic system integration (placeholder)"""
        # This is a placeholder for more complex integration tests
        assert True, "System integration test placeholder"
