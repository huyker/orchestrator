import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.engine import OrchestratorEngine
from orchestrator.models import Settings, iter_commands

class ProtocolEventTests(unittest.TestCase):
    def test_json_event_review_pass(self):
        comment = """
[issue1_review_pass_byGPT]

```orchestrator-event
{
  "schema_version": 1,
  "event_id": "issue1-pass-1",
  "issue_id": "issue1",
  "event": "review_pass",
  "actor": "GPT",
  "revision": 3,
  "review_cycle": 2,
  "pr": 15,
  "head_sha": "def456"
}
```
        """
        cmds = list(iter_commands(comment))
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertEqual(cmd["command"], "external_review")
        self.assertEqual(cmd["verdict"], "PASS")
        self.assertEqual(cmd["revision"], 3)
        self.assertEqual(cmd["review_cycle"], 2)
        self.assertEqual(cmd["pr_head_sha"], "def456")

    def test_json_event_review_fix(self):
        comment = """
```orchestrator-event
{
  "schema_version": 1,
  "event": "review_fix",
  "actor": "GPT",
  "revision": 3,
  "review_cycle": 2,
  "pr": 15,
  "head_sha": "def456",
  "findings": ["Bug 1", "Bug 2"]
}
```
        """
        cmds = list(iter_commands(comment))
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertEqual(cmd["command"], "external_review")
        self.assertEqual(cmd["verdict"], "FIX_REQUIRED")
        self.assertEqual(cmd["revision"], 3)

    def test_text_header_review_pass(self):
        comment = """
[issue1_review_pass_byGPT]

revision: 4
review_cycle: 3
pr: 17
head_sha: xyz789

Verdict: PASS
        """
        cmds = list(iter_commands(comment))
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertEqual(cmd["command"], "external_review")
        self.assertEqual(cmd["verdict"], "PASS")
        self.assertEqual(cmd["revision"], 4)
        self.assertEqual(cmd["review_cycle"], 3)
        self.assertEqual(cmd["pr_head_sha"], "xyz789")

    def test_text_header_answer(self):
        comment = """
[issue1_answer_byGPT]

question_id: targeting-rule
revision: 3

Use nearest target first.
        """
        cmds = list(iter_commands(comment))
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertEqual(cmd["command"], "answer")
        self.assertEqual(cmd["question_id"], "targeting-rule")
        self.assertEqual(cmd["revision"], 3)

    def test_legacy_command_block_still_works(self):
        comment = """
```orchestrator-command
{
  "command": "answer",
  "revision": 1,
  "question_id": "q1",
  "answer": "yes"
}
```
        """
        cmds = list(iter_commands(comment))
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["command"], "answer")
        self.assertEqual(cmds[0]["revision"], 1)

    def test_handoff_generation_takes_logical_issue_id_from_task_contract(self):
        """Handoff generation takes the logical issue_id from the orchestrator-task contract
        and cannot silently substitute the GitHub numeric Issue number.
        Case: GitHub Issue #8 maps to logical issue4 -> marker and JSON both use issue4.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "projects.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "projects": [{"id": "gamegit", "repo": "huyker/game", "default_branch": "main"}]
            }))
            settings = Settings(
                control_repo="huyker/game", token="test-token", registry_file=registry,
                runtime_dir=root / "runtime", workspace_root=root / "repos", poll_interval=5,
                git_transport="ssh", agy_bin="agy", agent_effort="medium", agent_timeout=30,
                test_timeout=2, lease_timeout=10, dashboard_host="127.0.0.1", dashboard_port=0,
                allowed_authors=("huyker",),
            )
            engine = OrchestratorEngine(settings)

            # Contract from Issue #8 declaring issue_id: "issue4"
            task_contract = {
                "schema_version": 1,
                "revision": 2,
                "task_id": "GAME-GPV1-DEMO-0004",
                "project": "gamegit",
                "target_repo": "huyker/game",
                "base_branch": "main",
                "type": "gameplay",
                "title": "Build HTML/JS placeholder gameplay demo and debug renderer",
                "objective": "Provide browser demo for validating Gameplay V1",
                "issue_id": "issue4",
                "condition": ["issue2", "issue3"]
            }

            # 1. Direct handoff formatting with task contract
            marker, body, comment = engine.format_handoff(
                issue=8,
                task=task_contract,
                pr_url="https://github.com/huyker/game/pull/13",
                pr_number=13,
                pr_head_sha="3890eabe95b0902de1ba0111c4b194632168b6d1",
                review_cycle=2,
            )

            # Assert emitted marker uses logical issue4, NOT GitHub numeric issue8
            self.assertEqual(marker, "[issue4_fixdone_byAGY]")
            self.assertNotEqual(marker, "[issue8_fixdone_byAGY]")

            # Assert JSON body uses logical issue4, NOT GitHub numeric issue8
            self.assertEqual(body["issue_id"], "issue4")
            self.assertNotEqual(body["issue_id"], "issue8")
            self.assertEqual(body["event"], "fixdone")
            self.assertEqual(body["actor"], "AGY")
            self.assertEqual(body["pr_number"], 13)
            self.assertEqual(body["pr_head_sha"], "3890eabe95b0902de1ba0111c4b194632168b6d1")
            self.assertEqual(body["review_cycle"], 2)

            # Assert emitted comment body contains the exact marker and JSON with issue4
            self.assertIn("[issue4_fixdone_byAGY]", comment)
            self.assertIn('"issue_id": "issue4"', comment)
            self.assertNotIn("[issue8_fixdone", comment)
            self.assertNotIn('"issue_id": "issue8"', comment)

            # 2. Multi-lease scenario: another task (e.g. Issue 9) has an earlier lease
            engine.state.force_claim("inst-9", 9, "WAITING_GPT_REVIEW", {"logical_issue_id": "issue5"})
            engine.state.force_claim("inst-8", 8, "RUNNING", {"logical_issue_id": "issue4"})

            marker2, body2, comment2 = engine.format_handoff(
                issue=8,
                task=None,  # Resolves from lease without task contract passed
                pr_number=13,
                pr_head_sha="3890eabe95b0902de1ba0111c4b194632168b6d1",
                review_cycle=2,
            )
            self.assertEqual(marker2, "[issue4_fixdone_byAGY]")
            self.assertEqual(body2["issue_id"], "issue4")
            self.assertNotIn("issue8", marker2)
            self.assertNotIn('"issue_id": "issue8"', comment2)

            # 3. Canonical ID parsing from issue body task contract
            issue_dict = {
                "number": 8,
                "title": "Build HTML/JS placeholder gameplay demo and debug renderer",
                "body": "## Task contract\n```orchestrator-task\n" + json.dumps(task_contract) + "\n```"
            }
            parsed_id = engine._parse_canonical_id(issue_dict)
            self.assertEqual(parsed_id, "issue4")
            self.assertNotEqual(parsed_id, "issue8")


if __name__ == "__main__":
    unittest.main()
