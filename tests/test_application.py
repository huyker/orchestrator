import json
import tempfile
import threading
import unittest
from pathlib import Path

from orchestrator.application import AllInOneApplication
from orchestrator.models import Settings


def settings_for(td):
    root = Path(td)
    registry = root / "projects.json"
    registry.write_text(json.dumps({
        "schema_version": 1,
        "projects": [{"id": "p", "repo": "o/r", "issues_repo": "o/r", "default_branch": "main"}],
    }))
    return Settings(
        control_repo="o/control",
        token="x",
        registry_file=registry,
        runtime_dir=root / "runtime",
        workspace_root=root / "repos",
        poll_interval=5,
        git_transport="ssh",
        agy_bin="agy",
        agent_effort="medium",
        agent_timeout=30,
        test_timeout=2,
        lease_timeout=10,
        dashboard_host="127.0.0.1",
        dashboard_port=0,
        allowed_authors=("owner",),
    )


class AllInOneApplicationTests(unittest.TestCase):
    def test_worker_waits_for_first_successful_sync(self):
        with tempfile.TemporaryDirectory() as td:
            app = AllInOneApplication(settings_for(td))
            calls = []
            app.engine.ensure_labels = lambda: calls.append("labels")
            app.engine.serve_loop = lambda stop: calls.append("worker")
            app.engine.github_auth_status = lambda: {"connected": True, "login": "owner"}

            thread = threading.Thread(target=app._worker_after_sync)
            thread.start()
            self.assertEqual(calls, [])

            app.projects_ready.set()
            thread.join(timeout=2)

            self.assertEqual(calls, ["labels", "worker"])

    def test_worker_waits_for_github_connection(self):
        with tempfile.TemporaryDirectory() as td:
            app = AllInOneApplication(settings_for(td))
            calls = []
            auth = {"connected": False}
            app.engine.github_auth_status = lambda: dict(auth)
            app.engine.ensure_labels = lambda: calls.append("labels")
            app.engine.serve_loop = lambda stop: calls.append("worker")
            app.projects_ready.set()

            thread = threading.Thread(target=app._worker_after_sync)
            thread.start()
            threading.Event().wait(0.05)
            self.assertEqual(calls, [])

            auth["connected"] = True
            thread.join(timeout=2)
            self.assertEqual(calls, ["labels", "worker"])

    def test_successful_auto_sync_enables_projects_ready(self):
        with tempfile.TemporaryDirectory() as td:
            app = AllInOneApplication(settings_for(td))

            def sync_projects():
                app.stop.set()
                return [{"id": "p", "ok": True}]

            app.engine.sync_projects = sync_projects
            app._sync_forever()

            self.assertTrue(app.projects_ready.is_set())

    def test_failed_auto_sync_does_not_enable_worker_gate(self):
        with tempfile.TemporaryDirectory() as td:
            app = AllInOneApplication(settings_for(td))

            def sync_projects():
                app.stop.set()
                return [{"id": "p", "ok": False, "error": "offline"}]

            app.engine.sync_projects = sync_projects
            app._sync_forever()

            self.assertFalse(app.projects_ready.is_set())


if __name__ == "__main__":
    unittest.main()
