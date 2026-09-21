import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from orchestrator.models import Settings, canonical_task_hash, parse_task
from orchestrator.project import Registry, github_repo_from_source, load_catalog, safe_path
from orchestrator.state import StateStore


class ModelsStateTests(unittest.TestCase):
    def test_settings_do_not_require_control_repo_and_infer_author(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "projects.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "projects": [
                    {
                        "id": "gamegit",
                        "repo": "huyker/game",
                        "issues_repo": "huyker/game",
                        "default_branch": "main"
                    }
                ]
            }))
            with patch.dict(os.environ, {
                "ORCH_PROJECT_REGISTRY": str(registry),
                "ORCH_RUNTIME_DIR": str(Path(td) / "runtime"),
                "ORCH_MANAGED_ROOT": str(Path(td) / "managed"),
            }, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.control_repo, "")
            self.assertEqual(settings.allowed_authors, ("huyker",))

    def test_settings_use_no_orchestrator_token(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "projects.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "projects": [{"id": "gamegit", "repo": "huyker/game", "issues_repo": "huyker/game"}]
            }))
            with patch.dict(os.environ, {
                "ORCH_PROJECT_REGISTRY": str(registry),
                "ORCH_RUNTIME_DIR": str(Path(td) / "runtime"),
                "ORCH_MANAGED_ROOT": str(Path(td) / "managed"),
            }, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.token, "")
            self.assertEqual(settings.allowed_authors, ("huyker",))

    def test_parse_exactly_one_task_block(self):
        body = '```orchestrator-task\n{"schema_version":1,"revision":1,"task_id":"T1","project":"p","type":"code","title":"x","objective":"y"}\n```'
        self.assertEqual(parse_task(body)["task_id"], "T1")
        with self.assertRaises(ValueError):
            parse_task(body + "\n" + body)

    def test_task_hash_changes_on_same_revision_mutation(self):
        task = {"schema_version":1,"revision":1,"task_id":"T","project":"p","type":"code","title":"x","objective":"a"}
        first = canonical_task_hash(task)
        task["objective"] = "b"
        self.assertNotEqual(first, canonical_task_hash(task))

    def test_registry_multi_project(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "projects.json"
            path.write_text(json.dumps({"schema_version":1,"projects":[
                {"id":"gamegit","repo":"huyker/game","default_branch":"main"},
                {"id":"other","repo":"acme/other","default_branch":"develop"}
            ]}))
            registry = Registry(path)
            self.assertEqual(registry.resolve("gamegit")["repo"], "huyker/game")
            self.assertEqual(registry.resolve("other")["default_branch"], "develop")

    def test_registry_can_add_managed_project_from_github_repo(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "projects.json"
            path.write_text(json.dumps({"schema_version":1,"projects":[]}))
            registry = Registry(path)
            entry = registry.add_project("git@github.com:acme/demo.git")
            self.assertEqual(entry["id"], "demo")
            self.assertEqual(entry["repo"], "acme/demo")
            saved = json.loads(path.read_text())
            self.assertEqual(saved["projects"][0]["issues_repo"], "acme/demo")

    def test_local_folder_is_not_a_valid_project_input(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                github_repo_from_source(str(Path(td)))

    def test_github_url_is_normalized_to_owner_repo(self):
        self.assertEqual(
            github_repo_from_source("https://github.com/acme/game.git")[0],
            "acme/game",
        )

    def test_catalog_loads_agents_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".orchestrator/agents").mkdir(parents=True)
            (root / ".orchestrator/tasks").mkdir(parents=True)
            (root / ".orchestrator/project.json").write_text(json.dumps({
                "schema_version":3,"project":"p","repository":"o/r",
                "agent_profiles_dir":".orchestrator/agents","task_profiles_dir":".orchestrator/tasks"
            }))
            (root / ".orchestrator/agents/e.json").write_text('{"id":"e","role":"executor","agy_agent":"exec"}')
            (root / ".orchestrator/agents/r.json").write_text('{"id":"r","role":"reviewer","agy_agent":"review"}')
            (root / ".orchestrator/tasks/c.json").write_text('{"id":"c","task_types":["code"],"executor_profile":"e","reviewer_profile":"r"}')
            catalog = load_catalog(root, ".orchestrator/project.json")
            self.assertIn("e", catalog["agents"])
            self.assertIn("c", catalog["tasks"])

    def test_safe_path_rejects_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(ValueError):
                safe_path(root, "../secret")

    def test_atomic_single_lease_across_connections(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.sqlite3"
            a = StateStore(path)
            b = StateStore(path)
            try:
                self.assertTrue(a.claim("a", 1, "RUNNING", {"x":1}))
                self.assertFalse(b.claim("b", 2, "RUNNING", {"x":2}))
                self.assertEqual(b.get_lease()["issue_number"], 1)
            finally:
                a.close()
                b.close()

    def test_stale_lease_takeover(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.sqlite3"
            a = StateStore(path)
            b = StateStore(path)
            try:
                self.assertTrue(a.claim("a", 1, "RUNNING", {"x":1}))
                a.db.execute("UPDATE lease SET heartbeat=? WHERE singleton=1", (time.time()-1000,))
                lease = b.takeover_if_stale("b", 10)
                self.assertEqual(lease["instance_id"], "b")
                self.assertEqual(lease["status"], "RECOVERING")
            finally:
                a.close()
                b.close()

    def test_command_consumed_once(self):
        with tempfile.TemporaryDirectory() as td:
            state = StateStore(Path(td) / "state.sqlite3")
            try:
                self.assertTrue(state.consume_command(10, 1, "answer"))
                self.assertFalse(state.consume_command(10, 1, "answer"))
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
