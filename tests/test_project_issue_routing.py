import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.engine import OrchestratorEngine
from orchestrator.models import Settings, canonical_task_hash


def settings_for(td):
    root = Path(td)
    registry = root / "projects.json"
    registry.write_text(json.dumps({
        "schema_version": 1,
        "projects": [{
            "id": "gamegit",
            "repo": "huyker/game",
            "issues_repo": "huyker/game",
            "default_branch": "main",
        }],
    }))
    return Settings(
        control_repo="huyker/orchestrator",
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
        allowed_authors=("huyker",),
    )


class ProjectIssueRoutingTests(unittest.TestCase):
    def test_scheduler_claims_from_project_issue_repo(self):
        with tempfile.TemporaryDirectory() as td:
            engine = OrchestratorEngine(settings_for(td))
            body = """```orchestrator-task
{"schema_version":1,"revision":1,"task_id":"G1","project":"gamegit","type":"gameplay","title":"x","objective":"y"}
```"""
            issue = {
                "number": 7,
                "title": "Game task",
                "body": body,
                "user": {"login": "huyker"},
                "labels": [{"name": "orch:ready"}],
            }
            seen_repos = []
            engine.github.list_ready_issues = lambda repo: seen_repos.append(repo) or [issue]
            engine.set_label = lambda *a, **k: None
            engine.event = lambda *a, **k: 1
            captured = {}
            engine._handle_active = lambda lease: captured.update(lease)

            engine.tick()
            self.assertEqual(seen_repos, [])

            engine.state.mark_dashboard_verified("http://127.0.0.1:8766")
            engine.tick()

            self.assertEqual(seen_repos, ["huyker/game"])
            self.assertEqual(captured["issue_number"], 7)
            self.assertEqual(captured["payload"]["issue_repo"], "huyker/game")
            self.assertEqual(captured["payload"]["task_id"], "G1")

    def test_gate_approval_binds_digest_and_pr_head(self):
        with tempfile.TemporaryDirectory() as td:
            engine = OrchestratorEngine(settings_for(td))
            task = {"revision": 1}
            payload = {
                "issue_repo": "huyker/game",
                "revision": 1,
                "contract_hash": canonical_task_hash(task),
                "target_repo": "huyker/game",
                "pending_gate_id": "asset-concept",
                "pending_gate_digest": "digest-123",
                "pending_gate_pr_head_sha": "abc123",
                "pr_number": 9,
                "command_after_comment_id": 50,
                "approved_gates": {},
            }
            self.assertTrue(engine.state.claim(engine.instance_id, 7, "WAITING_USER_GATE", payload))
            lease = engine.state.get_lease()
            engine._load_issue_task = lambda issue_number: ({}, task, canonical_task_hash(task))
            engine.github.get_pr = lambda repo, number: {"head": {"sha": "abc123"}}
            engine.github.comments = lambda repo, number: [{
                "id": 60,
                "user": {"login": "huyker"},
                "body": """```orchestrator-command
{"command":"approve_gate","revision":1,"gate_id":"asset-concept","artifact_digest":"digest-123","pr_head_sha":"abc123"}
```""",
            }]
            engine.event = lambda *a, **k: 61
            engine.set_label = lambda *a, **k: None

            engine._handle_active(lease)

            current = engine.state.get_lease()
            self.assertEqual(current["status"], "REWORK")
            self.assertEqual(
                current["payload"]["approved_gates"]["asset-concept"],
                {"artifact_digest": "digest-123", "pr_head_sha": "abc123"},
            )


if __name__ == "__main__":
    unittest.main()
