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
Integration tests for horus-runtime.
"""

import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.integration
class TestHorusRuntimeIntegration:
    """
    Integration test cases.
    """

    def test_main_script_execution(self) -> None:
        """
        Test that the main script runs and exposes the run command.
        """
        result = subprocess.run(
            ["horus", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0
        assert "run" in result.stdout

    @pytest.mark.slow
    def test_system_integration(self) -> None:
        """
        Test basic system integration (placeholder).
        """
        # This is a placeholder for more complex integration tests
        assert True, "System integration test placeholder"


def _chain_yaml(tmp_path: Path) -> dict[str, object]:
    """``split -> a -> b -> c``, b and c chained; a's slot 2 is slow."""
    log = tmp_path / "log"

    def stage(map_id: str, source: str, *, chain: bool) -> dict[str, object]:
        body = (
            f'echo "{map_id} $(basename $out) start $(date +%s.%N)" >> {log}; '
            f"[ {map_id} = a ] && [ $(basename $out) = 2 ] && sleep 1; "
            "{ if [ -d $item ]; then cat $item/r; else cat $item; fi; "
            f'echo "{map_id}"; }} > $out/r; '
            f'echo "{map_id} $(basename $out) end $(date +%s.%N)" >> {log}'
        )
        return {
            "id": map_id,
            "name": map_id,
            "kind": "horus_map",
            "chain": chain,
            "max_concurrency": 0,
            "over": {"input_id": "in", "as": "item"},
            "inputs": [{"kind": "folder", "id": "in", "path": f"{map_id}_in"}],
            "outputs": [
                {"kind": "folder", "id": "out", "path": f"{tmp_path}/{map_id}"}
            ],
            "runtime": {"kind": "command", "command": body},
            "executor": {"kind": "shell"},
            "target": {"kind": "local"},
            "_source": source,
        }

    maps = [
        stage("a", "split", chain=False),
        stage("b", "a", chain=True),
        stage("c", "b", chain=True),
    ]
    edges = [
        {
            "source": m.pop("_source"),
            "source_output": "batches" if m["id"] == "a" else "out",
            "target": m["id"],
            "target_input": "in",
        }
        for m in maps
    ]
    split = {
        "id": "split",
        "name": "split",
        "kind": "horus_task",
        "runtime": {
            "kind": "command",
            "command": "for n in x y z; do echo $n > $batches/$n; done",
        },
        "executor": {"kind": "shell"},
        "target": {"kind": "local"},
        "outputs": [
            {"kind": "folder", "id": "batches", "path": f"{tmp_path}/batches"}
        ],
    }
    return {
        "name": "chain_smoke",
        "kind": "horus_workflow",
        "tasks": [split, *maps],
        "edges": edges,
    }


@pytest.mark.integration
def test_horus_run_streams_a_three_map_chain(tmp_path: Path) -> None:
    """
    ``horus run`` on a YAML chain: b's first item starts before a's slow
    last item ends, and every item reaches c.
    """
    (tmp_path / "batches").mkdir()
    workflow = tmp_path / "chain.yaml"
    workflow.write_text(yaml.safe_dump(_chain_yaml(tmp_path)))

    result = subprocess.run(
        ["horus", "run", str(workflow), "--no-tui", "--trigger", "split"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    times = {
        (m, slot, what): float(t)
        for m, slot, what, t in (
            line.split()
            for line in (tmp_path / "log").read_text().splitlines()
        )
    }
    assert times[("b", "0", "start")] < times[("a", "2", "end")]
    for slot, name in enumerate("xyz"):
        assert (tmp_path / "c" / str(slot) / "r").read_text() == (
            f"{name}\na\nb\nc\n"
        )
