import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.core import Registry, State, load_catalog, parse_task


class CoreTests(unittest.TestCase):
    def test_parse_task(self):
        body = '```orchestrator-task\n{"schema_version":1,"revision":1,"task_id":"T1","project":"p","type":"code","title":"x","objective":"y"}\n```'
        self.assertEqual(parse_task(body)["task_id"], "T1")

    def test_registry_multi_project(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "projects.json"
            p.write_text(json.dumps({"schema_version":1,"projects":[
                {"id":"gamegit","repo":"huyker/game","default_branch":"main"},
                {"id":"other","repo":"acme/other","default_branch":"develop"}
            ]}))
            r = Registry(p)
            self.assertEqual(r.resolve("gamegit")["repo"], "huyker/game")
            self.assertEqual(r.resolve("other")["default_branch"], "develop")

    def test_single_active_state(self):
        with tempfile.TemporaryDirectory() as td:
            s = State(Path(td) / "state.sqlite3")
            self.assertIsNone(s.get())
            s.set({"issue_number":1,"status":"RUNNING"})
            self.assertEqual(s.get()["status"], "RUNNING")
            s.clear()
            self.assertIsNone(s.get())

    def test_load_project_catalog(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".orchestrator/agents").mkdir(parents=True)
            (root / ".orchestrator/tasks").mkdir(parents=True)
            (root / ".orchestrator/project.json").write_text(json.dumps({
                "schema_version":2,"project":"p","repository":"o/r","default_branch":"main",
                "agent_profiles_dir":".orchestrator/agents","task_profiles_dir":".orchestrator/tasks"
            }))
            (root / ".orchestrator/agents/e.json").write_text('{"id":"e","role":"executor","agy_agent":"03_executor"}')
            (root / ".orchestrator/agents/r.json").write_text('{"id":"r","role":"reviewer","agy_agent":"04_reviewer"}')
            (root / ".orchestrator/tasks/c.json").write_text('{"id":"c","task_types":["code"],"executor_profile":"e","reviewer_profile":"r"}')
            c = load_catalog(root, ".orchestrator/project.json")
            self.assertIn("e", c["agents"])
            self.assertIn("c", c["tasks"])


if __name__ == "__main__":
    unittest.main()
