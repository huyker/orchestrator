import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.graphify_adapter import GraphifyAdapter


class GraphifyAdapterTests(unittest.TestCase):
    def _fake_graphify(self, root: Path) -> Path:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        script = bin_dir / "graphify"
        script.write_text(
            """#!/usr/bin/env python3
import json
import sys
from pathlib import Path
root = Path.cwd()
(root / 'graphify-calls.log').open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')
args = sys.argv[1:]
if args and args[0] == 'query':
    print('GRAPH CONTEXT: Foundation -> RunePlatform -> Hero')
    raise SystemExit(0)
out = root / 'graphify-out'
out.mkdir(exist_ok=True)
(out / 'graph.json').write_text('{"nodes": [], "edges": []}', encoding='utf-8')
print('graph ready')
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return bin_dir

    def test_build_update_and_query(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bin_dir = self._fake_graphify(root)
            manifest = {
                "graphify": {
                    "enabled": True,
                    "required": True,
                    "auto_update": "before_task",
                    "query_context": True,
                    "output_dir": "graphify-out",
                }
            }
            with patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}):
                adapter = GraphifyAdapter()
                first = adapter.ensure_graph(root, manifest)
                self.assertTrue(first["graph_exists"])
                second = adapter.ensure_graph(root, manifest)
                self.assertTrue(second["graph_exists"])
                context = adapter.query(root, manifest, "Foundation flow")
                self.assertIn("Foundation -> RunePlatform -> Hero", context)

            calls = [json.loads(x) for x in (root / "graphify-calls.log").read_text().splitlines()]
            self.assertEqual(calls[0], [".", "--no-viz"])
            self.assertEqual(calls[1], ["update", "."])
            self.assertEqual(calls[2][0:2], ["query", "Foundation flow"])
            self.assertIn("--graph", calls[2])

    def test_optional_missing_cli_is_fail_open(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {"graphify": {"enabled": True, "required": False}}
            with patch("orchestrator.graphify_adapter.shutil.which", return_value=None):
                status = GraphifyAdapter().ensure_graph(root, manifest)
            self.assertFalse(status["cli_available"])
            self.assertFalse(status["graph_exists"])

    def test_required_missing_cli_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = {"graphify": {"enabled": True, "required": True}}
            with patch("orchestrator.graphify_adapter.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "required"):
                    GraphifyAdapter().ensure_graph(root, manifest)


if __name__ == "__main__":
    unittest.main()
