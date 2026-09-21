import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.engine import OrchestratorEngine
from orchestrator.models import (
    LABEL_BLOCKED,
    LABEL_DONE,
    LABEL_READY,
    LABEL_RUNNING,
    LABEL_WAITING_CONDITION,
    Settings,
    parse_task,
)


def temp_dir():
    try:
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    except TypeError:
        return tempfile.TemporaryDirectory()


def settings_for(td, max_workers: int = 2):
    root = Path(td)
    registry = root / "projects.json"
    registry.write_text(
        json.dumps({
            "schema_version": 1,
            "projects": [
                {
                    "id": "gamegit",
                    "repo": "huyker/game",
                    "default_branch": "main",
                    "issues_repo": "huyker/game",
                }
            ],
        })
    )
    return Settings(
        control_repo="huyker/game",
        token="test-token",
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
        agy_model="gemini-3.8-flash-high",
        max_workers=max_workers,
    )


class MultiAgentConditionTests(unittest.TestCase):
    def test_model_inheritance_and_per_agent_override(self):
        with temp_dir() as td:
            engine = OrchestratorEngine(settings_for(td))
            try:
                # Default is gemini-3.8-flash-high
                self.assertEqual(engine.get_agy_model(), "gemini-3.8-flash-high")
                agent = {"id": "game-executor", "role": "executor"}
                # Inherits default
                self.assertEqual(engine.resolve_agent_model(agent), "gemini-3.8-flash-high")

                # Override for game-executor
                engine.set_agent_model("game-executor", "claude-sonnet-4-6")
                self.assertEqual(engine.get_agent_model("game-executor"), "claude-sonnet-4-6")
                self.assertEqual(engine.resolve_agent_model(agent), "claude-sonnet-4-6")

                # Other agent still inherits default
                reviewer = {"id": "game-reviewer", "role": "reviewer"}
                self.assertEqual(engine.resolve_agent_model(reviewer), "gemini-3.8-flash-high")

                # Clear override -> returns to default
                engine.set_agent_model("game-executor", None)
                self.assertIsNone(engine.get_agent_model("game-executor"))
                self.assertEqual(engine.resolve_agent_model(agent), "gemini-3.8-flash-high")

                # Change global default -> all unconfigured agents get the new default
                engine.set_agy_model("gemini-3.1-pro-high")
                self.assertEqual(engine.resolve_agent_model(agent), "gemini-3.1-pro-high")
                self.assertEqual(engine.resolve_agent_model(reviewer), "gemini-3.1-pro-high")
            finally:
                engine.close()

    def test_independent_task_condition_satisfied(self):
        with temp_dir() as td:
            engine = OrchestratorEngine(settings_for(td))
            try:
                task = {"task_id": "T1", "condition": []}
                satisfied, reason, is_invalid = engine._evaluate_task_condition(task, "issue1", [])
                self.assertTrue(satisfied)
                self.assertFalse(is_invalid)
            finally:
                engine.close()

    def test_self_dependency_rejected(self):
        with temp_dir() as td:
            engine = OrchestratorEngine(settings_for(td))
            try:
                task = {"task_id": "T1", "condition": ["issue1"]}
                satisfied, reason, is_invalid = engine._evaluate_task_condition(task, "issue1", [])
                self.assertFalse(satisfied)
                self.assertTrue(is_invalid)
                self.assertIn("self-dependency", reason)
            finally:
                engine.close()

    def test_dependency_cycle_rejected(self):
        with temp_dir() as td:
            engine = OrchestratorEngine(settings_for(td))
            try:
                issue1 = {
                    "number": 1,
                    "title": "[issue1] Task 1",
                    "state": "open",
                    "labels": [{"name": "orch:ready"}],
                    "body": "```orchestrator-task\n"
                    + json.dumps({
                        "schema_version": 1,
                        "revision": 1,
                        "task_id": "T1",
                        "project": "gamegit",
                        "type": "code",
                        "title": "T1",
                        "objective": "o",
                        "condition": ["issue2"],
                    })
                    + "\n```",
                }
                issue2 = {
                    "number": 2,
                    "title": "[issue2] Task 2",
                    "state": "open",
                    "labels": [{"name": "orch:ready"}],
                    "body": "```orchestrator-task\n"
                    + json.dumps({
                        "schema_version": 1,
                        "revision": 1,
                        "task_id": "T2",
                        "project": "gamegit",
                        "type": "code",
                        "title": "T2",
                        "objective": "o",
                        "condition": ["issue1"],
                    })
                    + "\n```",
                }
                all_issues = [issue1, issue2]
                task1 = parse_task(issue1["body"])
                satisfied, reason, is_invalid = engine._evaluate_task_condition(task1, "issue1", all_issues)
                self.assertFalse(satisfied)
                self.assertTrue(is_invalid)
                self.assertIn("cycle", reason.lower())
            finally:
                engine.close()

    def test_condition_satisfaction_when_dependency_becomes_done(self):
        with temp_dir() as td:
            engine = OrchestratorEngine(settings_for(td))
            try:
                # Issue 1 is open (not done)
                issue1 = {
                    "number": 1,
                    "title": "[issue1] Task 1",
                    "state": "open",
                    "labels": [{"name": "orch:running"}],
                    "body": "```orchestrator-task\n"
                    + json.dumps({
                        "schema_version": 1,
                        "revision": 1,
                        "task_id": "T1",
                        "project": "gamegit",
                        "type": "code",
                        "title": "T1",
                        "objective": "o",
                        "condition": [],
                    })
                    + "\n```",
                }
                issue2 = {
                    "number": 2,
                    "title": "[issue2] Task 2",
                    "state": "open",
                    "labels": [{"name": "orch:waiting-condition"}],
                    "body": "```orchestrator-task\n"
                    + json.dumps({
                        "schema_version": 1,
                        "revision": 1,
                        "task_id": "T2",
                        "project": "gamegit",
                        "type": "code",
                        "title": "T2",
                        "objective": "o",
                        "condition": ["issue1"],
                    })
                    + "\n```",
                }
                all_issues = [issue1, issue2]
                task2 = parse_task(issue2["body"])

                # Condition is not satisfied because issue1 is not done
                satisfied, reason, is_invalid = engine._evaluate_task_condition(task2, "issue2", all_issues)
                self.assertFalse(satisfied)
                self.assertFalse(is_invalid)

                # Now issue 1 reaches terminal state DONE
                issue1["state"] = "closed"
                issue1["labels"] = [{"name": "orch:done"}]

                # Condition is now satisfied!
                satisfied, reason, is_invalid = engine._evaluate_task_condition(task2, "issue2", all_issues)
                self.assertTrue(satisfied)
                self.assertFalse(is_invalid)
            finally:
                engine.close()

    def test_multi_lease_capacity_up_to_max_workers(self):
        with temp_dir() as td:
            engine = OrchestratorEngine(settings_for(td, max_workers=2))
            try:
                # Claim task 1
                payload1 = {"revision": 1, "task_id": "T1", "contract_hash": "h1"}
                self.assertTrue(engine.state.claim("inst1", 1, "RUNNING", payload1, max_workers=2))
                self.assertEqual(engine.state.active_lease_count(), 1)

                # Claim task 2 (within capacity of 2)
                payload2 = {"revision": 1, "task_id": "T2", "contract_hash": "h2"}
                self.assertTrue(engine.state.claim("inst1", 2, "RUNNING", payload2, max_workers=2))
                self.assertEqual(engine.state.active_lease_count(), 2)

                # Attempt task 3 (exceeds capacity of 2)
                payload3 = {"revision": 1, "task_id": "T3", "contract_hash": "h3"}
                self.assertFalse(engine.state.claim("inst1", 3, "RUNNING", payload3, max_workers=2))
                self.assertEqual(engine.state.active_lease_count(), 2)

                # Retrieve leases individually
                l1 = engine.state.get_lease(1)
                l2 = engine.state.get_lease(2)
                self.assertIsNotNone(l1)
                self.assertIsNotNone(l2)
                self.assertEqual(l1["issue_number"], 1)
                self.assertEqual(l2["issue_number"], 2)

                # Release task 1
                engine.state.release("inst1", issue_number=1)
                self.assertEqual(engine.state.active_lease_count(), 1)
                self.assertIsNone(engine.state.get_lease(1))
                self.assertIsNotNone(engine.state.get_lease(2))

                # Now task 3 can be claimed
                self.assertTrue(engine.state.claim("inst1", 3, "RUNNING", payload3, max_workers=2))
                self.assertEqual(engine.state.active_lease_count(), 2)
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
