import unittest
from orchestrator.models import iter_commands

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


if __name__ == "__main__":
    unittest.main()
