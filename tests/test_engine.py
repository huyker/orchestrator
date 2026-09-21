import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.engine import OrchestratorEngine
from orchestrator.models import Settings


def settings_for(td):
    root = Path(td)
    registry = root / "projects.json"
    registry.write_text(json.dumps({"schema_version":1,"projects":[{"id":"p","repo":"o/r","default_branch":"main"}]}))
    return Settings(
        control_repo="o/control", token="x", registry_file=registry,
        runtime_dir=root/"runtime", workspace_root=root/"repos", poll_interval=5,
        git_transport="ssh", agy_bin="agy", agent_effort="medium", agent_timeout=30,
        test_timeout=2, lease_timeout=10, dashboard_host="127.0.0.1", dashboard_port=0,
        allowed_authors=("owner",),
    )


class EngineTests(unittest.TestCase):
    def test_lifecycle_stage_mapping(self):
        ready = OrchestratorEngine._lifecycle("READY")
        validating = OrchestratorEngine._lifecycle("VALIDATING")
        review = OrchestratorEngine._lifecycle("WAITING_GPT_REVIEW")
        self.assertEqual(ready["stage_index"], 0)
        self.assertEqual(validating["stage_index"], 2)
        self.assertEqual(review["stage_index"], 4)
        self.assertEqual(review["progress_percent"], 85)

    def test_tick_is_hard_gated_until_dashboard_verified(self):
        with tempfile.TemporaryDirectory() as td:
            engine = OrchestratorEngine(settings_for(td))
            engine.github.list_ready_issues = lambda repo: (_ for _ in ()).throw(AssertionError("Issue queue must not be read"))
            engine.github.list_open_orchestrator_issues = lambda repo: (_ for _ in ()).throw(AssertionError("Issue status queue must not be read"))
            engine.tick()
            snapshot = engine.snapshot()
            self.assertFalse(engine.state.is_dashboard_verified())
            self.assertEqual(snapshot["issues"], [])
            self.assertIn("dashboard bootstrap not verified", snapshot["issues_error"])
            with self.assertRaises(RuntimeError):
                engine.ensure_labels()

    def test_question_answer_binding_and_replay_protection(self):
        with tempfile.TemporaryDirectory() as td:
            engine = OrchestratorEngine(settings_for(td))
            comments = [
                {"id": 10, "user":{"login":"owner"}, "body":'```orchestrator-command\n{"command":"answer","revision":1,"question_id":"old","answer":"x"}\n```'},
                {"id": 11, "user":{"login":"owner"}, "body":'```orchestrator-command\n{"command":"answer","revision":1,"question_id":"q1","answer":"yes"}\n```'},
            ]
            found = engine._find_command(comments, 1, "answer", 1, after_comment_id=9, predicate=lambda c:c.get("question_id")=="q1")
            self.assertEqual(found[0]["answer"], "yes")
            self.assertIsNone(engine._find_command(comments, 1, "answer", 1, after_comment_id=9, predicate=lambda c:c.get("question_id")=="q1"))

    def test_review_parser_returns_rework_errors(self):
        with tempfile.TemporaryDirectory() as td:
            engine = OrchestratorEngine(settings_for(td))
            task = {"acceptance":["A"],"prohibited":[],"review":{"min_score":100}}
            line = '@@ORCH_REVIEW@@ '+json.dumps({"verdict":"FAIL","score":50,"acceptance":[{"criterion":"A","status":"FAIL","evidence":"missing"}],"prohibited":[]})
            review, errors = engine.parse_review(line, task)
            self.assertEqual(review["verdict"], "FAIL")
            self.assertTrue(errors)

    def test_manifest_protected_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            engine = OrchestratorEngine(settings_for(td))
            engine.workspace.changed_files = lambda wt, base: [".github/workflows/pwn.yml"]
            task = {"base_branch":"main","type":"code","checks":{"test_profiles":[]}}
            catalog = {"manifest":{"protected_paths":[".github/workflows/**"],"test_profiles":{}},"agents":{},"tasks":{}}
            profile = {"require_substantive_diff":True}
            result = engine.machine_acceptance(task,catalog,profile,Path(td))
            self.assertFalse(result["pass"])
            self.assertIn("forbidden diff glob changed: .github/workflows/**", result["failures"])

    def test_asset_profile_requires_user_gate(self):
        with tempfile.TemporaryDirectory() as td:
            engine = OrchestratorEngine(settings_for(td))
            with self.assertRaises(ValueError):
                engine._required_gate({"user_gates":[]},{"require_user_gate":True},{"approved_gates":{}})
            gate = engine._required_gate({"user_gates":[{"id":"concept","required":True}]},{"require_user_gate":True},{"approved_gates":{}})
            self.assertEqual(gate["id"], "concept")

    def test_named_test_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            engine = OrchestratorEngine(settings_for(td))
            result = engine._run_named_test("python -c \"import time; time.sleep(2)\"", Path(td), 1)
            self.assertTrue(result["timed_out"])


if __name__ == "__main__":
    unittest.main()

class LifecycleTests(unittest.TestCase):
    def test_same_revision_contract_mutation_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            engine = OrchestratorEngine(settings_for(td))
            payload={"revision":1,"contract_hash":"old"}
            engine.state.claim(engine.instance_id, 1, "RUNNING", payload)
            lease=engine.state.get_lease()
            engine._block=lambda lease, reason, **extra: engine.state.update_lease(engine.instance_id,status="BLOCKED",payload={**lease["payload"],"reason":reason})
            _, ok=engine._reconcile_contract(lease,{}, {"revision":1}, "new")
            self.assertFalse(ok)
            self.assertEqual(engine.state.get_lease()["status"],"BLOCKED")

    def test_reviewer_failure_schedules_same_task_rework(self):
        with tempfile.TemporaryDirectory() as td:
            engine = OrchestratorEngine(settings_for(td))
            payload={"revision":1,"contract_hash":"h","rework_count":0}
            engine.state.claim(engine.instance_id, 1, "RUNNING", payload)
            lease=engine.state.get_lease()
            engine.event=lambda *a, **k: 50
            engine.set_label=lambda *a, **k: None
            engine._schedule_rework(lease,payload,{"review":{"max_rework":3}},"review_failed",errors=["x"])
            current=engine.state.get_lease()
            self.assertEqual(current["status"],"REWORK")
            self.assertEqual(current["issue_number"],1)
