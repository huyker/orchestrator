from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

from orchestrator.engine import OrchestratorEngine
from orchestrator.models import LABEL_DONE, LABEL_READY, LABEL_REWORK, LABEL_RUNNING, Settings
from orchestrator.project import github_repo_from_source, remove_prefix, remove_suffix
from orchestrator.state import StateStore


class TestStartupReconciliation(unittest.TestCase):
    def test_remove_suffix_and_remove_prefix(self):
        self.assertEqual(remove_suffix("project.git", ".git"), "project")
        self.assertEqual(remove_suffix("project", ".git"), "project")
        self.assertEqual(remove_suffix("project.git.git", ".git"), "project.git")
        self.assertEqual(remove_suffix("", ".git"), "")
        self.assertEqual(remove_suffix("project", ""), "project")

        self.assertEqual(remove_prefix("https://github.com/org/repo", "https://github.com/"), "org/repo")
        self.assertEqual(remove_prefix("org/repo", "https://github.com/"), "org/repo")
        self.assertEqual(remove_prefix("", "prefix"), "")
        self.assertEqual(remove_prefix("foo", ""), "foo")

    def test_github_repo_from_source_windows_and_edge_cases(self):
        # Basic owner/repo
        self.assertEqual(github_repo_from_source("acme/project")[0], "acme/project")
        # Windows backslashes
        self.assertEqual(github_repo_from_source("acme\\project")[0], "acme/project")
        # CRLF and quotes
        self.assertEqual(github_repo_from_source('  "acme/project.git"\r\n')[0], "acme/project")
        # Full URLs
        self.assertEqual(github_repo_from_source("https://github.com/acme/project.git")[0], "acme/project")
        self.assertEqual(github_repo_from_source("https://github.com/acme/project/")[0], "acme/project")
        self.assertEqual(github_repo_from_source("git@github.com:acme/project.git")[0], "acme/project")
        self.assertEqual(github_repo_from_source("ssh://git@github.com/acme/project.git")[0], "acme/project")

    def test_adopt_leases_on_startup(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = StateStore(db_path)

            # Claim lease 101 as RUNNING by instance 1
            store.claim("inst-1", 101, "RUNNING", {"task_id": "t1"}, max_workers=3)
            # Claim lease 102 as WAITING_ANSWER by instance 1
            store.claim("inst-1", 102, "WAITING_ANSWER", {"task_id": "t2"}, max_workers=3)

            # Start up new instance 2
            adopted = store.adopt_leases_on_startup("inst-2")
            self.assertEqual(len(adopted), 2)

            l101 = store.get_lease(101)
            self.assertIsNotNone(l101)
            self.assertEqual(l101["instance_id"], "inst-2")
            # RUNNING should be converted to REWORK because process was interrupted
            self.assertEqual(l101["status"], "REWORK")
            self.assertEqual(l101["payload"].get("recovered_from_instance"), "inst-1")

            l102 = store.get_lease(102)
            self.assertIsNotNone(l102)
            self.assertEqual(l102["instance_id"], "inst-2")
            self.assertEqual(l102["status"], "WAITING_ANSWER")

    def test_reconcile_startup_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reg_path = root / "projects.json"
            reg_path.write_text(json.dumps({
                "schema_version": 1,
                "projects": [
                    {
                        "id": "proj-1",
                        "repo": "owner/repo",
                        "issues_repo": "owner/repo",
                        "enabled": True,
                    }
                ]
            }), encoding="utf-8")

            settings = Settings(
                control_repo="",
                token="",
                registry_file=reg_path,
                runtime_dir=root / "runtime",
                workspace_root=root / "workspace",
                poll_interval=5,
                git_transport="https",
                agy_bin="mock-agy",
                agent_effort="low",
                agent_timeout=30,
                test_timeout=30,
                lease_timeout=180,
                dashboard_host="127.0.0.1",
                dashboard_port=0,
                allowed_authors=("test-user",),
                max_workers=2,
            )

            engine = OrchestratorEngine(settings)
            engine.github = MagicMock()

            # Mock existing lease 50 that was marked done on GitHub
            engine.state.claim("old-inst", 50, "RUNNING", {"task_id": "done-task"}, max_workers=2)

            # Mock GitHub issues returned
            engine.github.list_open_orchestrator_issues.return_value = [
                {
                    "number": 50,
                    "state": "closed",
                    "labels": [{"name": LABEL_DONE}],
                    "title": "Closed Task",
                    "body": "No task block",
                },
                {
                    "number": 100,
                    "state": "open",
                    "labels": [{"name": LABEL_REWORK}],
                    "title": "Rework Task",
                    "body": "```orchestrator-task\n{\"schema_version\":1,\"project\":\"proj-1\",\"type\":\"feature\",\"task_id\":\"task-100\",\"revision\":1,\"title\":\"T100\",\"objective\":\"Do 100\"}\n```",
                },
                {
                    "number": 101,
                    "state": "open",
                    "labels": [{"name": LABEL_RUNNING}],
                    "title": "Interrupted Task",
                    "body": "```orchestrator-task\n{\"schema_version\":1,\"project\":\"proj-1\",\"type\":\"bugfix\",\"task_id\":\"task-101\",\"revision\":1,\"title\":\"T101\",\"objective\":\"Do 101\"}\n```",
                },
            ]

            summary = engine.reconcile_startup_state()

            # Lease 50 should be released
            self.assertIsNone(engine.state.get_lease(50))
            self.assertEqual(summary["released_leases"], 1)

            # Issue 100 should be reconstructed with REWORK
            l100 = engine.state.get_lease(100)
            self.assertIsNotNone(l100)
            self.assertEqual(l100["status"], "REWORK")
            self.assertEqual(l100["instance_id"], engine.instance_id)

            # Issue 101 should be reconstructed with REWORK (interrupted running task)
            l101 = engine.state.get_lease(101)
            self.assertIsNotNone(l101)
            self.assertEqual(l101["status"], "REWORK")
            self.assertEqual(l101["instance_id"], engine.instance_id)

    def test_tick_picks_up_unleased_rework_issue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reg_path = root / "projects.json"
            reg_path.write_text(json.dumps({
                "schema_version": 1,
                "projects": [
                    {
                        "id": "proj-1",
                        "repo": "owner/repo",
                        "issues_repo": "owner/repo",
                        "enabled": True,
                    }
                ]
            }), encoding="utf-8")

            settings = Settings(
                control_repo="",
                token="",
                registry_file=reg_path,
                runtime_dir=root / "runtime",
                workspace_root=root / "workspace",
                poll_interval=5,
                git_transport="https",
                agy_bin="mock-agy",
                agent_effort="low",
                agent_timeout=30,
                test_timeout=30,
                lease_timeout=180,
                dashboard_host="127.0.0.1",
                dashboard_port=0,
                allowed_authors=("test-user",),
                max_workers=2,
            )

            engine = OrchestratorEngine(settings)
            engine.state.mark_dashboard_verified("http://127.0.0.1:8766")
            engine._github_auth = {"connected": True, "login": "test-user"}
            engine.github = MagicMock()
            engine._handle_active = MagicMock()

            engine.github.list_ready_issues.return_value = []
            engine.github.list_open_orchestrator_issues.return_value = [
                {
                    "number": 200,
                    "state": "open",
                    "labels": [{"name": LABEL_REWORK}],
                    "title": "Rework Task",
                    "user": {"login": "test-user"},
                    "body": "```orchestrator-task\n{\"schema_version\":1,\"project\":\"proj-1\",\"type\":\"feature\",\"task_id\":\"task-200\",\"revision\":1,\"title\":\"T200\",\"objective\":\"Do 200\"}\n```",
                }
            ]

            engine.tick()

            lease = engine.state.get_lease(200)
            self.assertIsNotNone(lease)
            self.assertEqual(lease["status"], "REWORK")
            engine._handle_active.assert_called_once()

    def test_git_local_task_issues_and_sync_datetime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reg_path = root / "projects.json"
            repo_dir = root / "workspace" / "owner" / "repo"
            repo_dir.mkdir(parents=True)
            import subprocess
            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "user.name", "tester"], cwd=repo_dir, check=True)
            (repo_dir / "README.md").write_text("hello")
            subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "task/issue-42-smoke-test"], cwd=repo_dir, check=True, capture_output=True)
            (repo_dir / "smoke.txt").write_text("smoke test")
            subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
            subprocess.run(["git", "commit", "-m", "task(SMOKE): issue #42"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True, capture_output=True)

            reg_path.write_text(json.dumps({
                "schema_version": 1,
                "projects": [
                    {
                        "id": "proj-1",
                        "repo": "owner/repo",
                        "issues_repo": "owner/repo",
                        "enabled": True,
                    }
                ]
            }), encoding="utf-8")

            settings = Settings(
                control_repo="",
                token="",
                registry_file=reg_path,
                runtime_dir=root / "runtime",
                workspace_root=root / "workspace",
                poll_interval=5,
                git_transport="https",
                agy_bin="mock-agy",
                agent_effort="low",
                agent_timeout=30,
                test_timeout=30,
                lease_timeout=180,
                dashboard_host="127.0.0.1",
                dashboard_port=0,
                allowed_authors=("test-user",),
                max_workers=2,
            )

            engine = OrchestratorEngine(settings)
            engine.state.mark_dashboard_verified("http://127.0.0.1:8766")
            snap = engine.snapshot()
            self.assertEqual(len(snap["issues"]), 1)
            self.assertEqual(snap["issues"][0]["number"], 42)
            self.assertEqual(snap["issues"][0]["title"], "task(SMOKE): issue #42")

            engine.sync_projects()
            self.assertIsNotNone(engine._last_sync_datetime)
            self.assertIsNotNone(engine._last_sync_at)
            snap2 = engine.snapshot()
            self.assertEqual(snap2["auto_sync"]["last_sync_datetime"], engine._last_sync_datetime)

    def test_token_persistence_and_git_config(self):
        from orchestrator.github_client import GitHubClient
        client = GitHubClient("")
        with mock.patch("subprocess.run") as mock_run:
            # Test fallback to git config
            mock_proc = mock.MagicMock(returncode=0, stdout="git_token_12345\n")
            mock_run.return_value = mock_proc
            token = client._get_token()
            self.assertEqual(token, "git_token_12345")

    def test_commit_registry_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "tester"], cwd=root, check=True)

            reg_path = root / "projects.json"
            reg_path.write_text(json.dumps({"schema_version": 1, "projects": []}), encoding="utf-8")
            subprocess.run(["git", "add", "projects.json"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "init registry"], cwd=root, check=True, capture_output=True)

            settings = Settings(
                control_repo="",
                token="",
                registry_file=reg_path,
                runtime_dir=root / "runtime",
                workspace_root=root / "workspace",
                poll_interval=5,
                git_transport="https",
                agy_bin="mock-agy",
                agent_effort="low",
                agent_timeout=30,
                test_timeout=30,
                lease_timeout=180,
                dashboard_host="127.0.0.1",
                dashboard_port=0,
                allowed_authors=("test-user",),
                max_workers=2,
            )
            engine = OrchestratorEngine(settings)
            reg_path.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p1"}]}), encoding="utf-8")
            engine._commit_registry_change("update test")
            log = subprocess.run(["git", "log", "-n", "1", "--oneline"], cwd=root, capture_output=True, text=True)
            self.assertIn("update test", log.stdout)


if __name__ == "__main__":
    unittest.main()



