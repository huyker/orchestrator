import json
import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from orchestrator.dashboard import make_server
from orchestrator.models import Settings
from orchestrator.state import StateStore
from orchestrator.engine import OrchestratorEngine


class FinalReviewerConfigTests(unittest.TestCase):
    def test_default_settings_final_reviewer(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Path(td) / "projects.json"
            reg.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p", "repo": "u/r"}]}))
            with patch.dict(os.environ, {
                "ORCH_PROJECT_REGISTRY": str(reg),
                "ORCH_RUNTIME_DIR": str(Path(td) / "runtime"),
                "ORCH_MANAGED_ROOT": str(Path(td) / "managed"),
            }, clear=True):
                settings = Settings.from_env()
                self.assertEqual(settings.final_reviewer, "chatgpt")
                self.assertEqual(settings.gemini_reviewer_model, "gemini-3.8-flash-high")

    def test_env_override_final_reviewer(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Path(td) / "projects.json"
            reg.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p", "repo": "u/r"}]}))
            with patch.dict(os.environ, {
                "ORCH_PROJECT_REGISTRY": str(reg),
                "ORCH_RUNTIME_DIR": str(Path(td) / "runtime"),
                "ORCH_MANAGED_ROOT": str(Path(td) / "managed"),
                "ORCH_FINAL_REVIEWER": "gemini",
                "ORCH_GEMINI_REVIEWER_MODEL": "gemini-3.8-flash-high",
            }, clear=True):
                settings = Settings.from_env()
                self.assertEqual(settings.final_reviewer, "gemini")
                self.assertEqual(settings.gemini_reviewer_model, "gemini-3.8-flash-high")

    def test_engine_final_reviewer_management(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Path(td) / "projects.json"
            reg.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p", "repo": "u/r"}]}))
            runtime = Path(td) / "runtime"
            settings = Settings(
                control_repo="",
                token="",
                registry_file=reg,
                runtime_dir=runtime,
                workspace_root=Path(td) / "managed",
                poll_interval=5,
                git_transport="ssh",
                agy_bin="agy",
                agent_effort="medium",
                agent_timeout=1800,
                test_timeout=600,
                lease_timeout=90,
                dashboard_host="127.0.0.1",
                dashboard_port=8766,
                allowed_authors=("u",),
                agy_model="gemini-3.8-flash-high",
                final_reviewer="chatgpt",
                gemini_reviewer_model="gemini-3.8-flash-high",
            )
            engine = OrchestratorEngine(settings)
            try:
                # Default is chatgpt
                self.assertEqual(engine.get_final_reviewer(), "chatgpt")
                self.assertEqual(engine.get_gemini_reviewer_model(), "gemini-3.8-flash-high")

                # Switch to gemini
                engine.set_final_reviewer("gemini")
                self.assertEqual(engine.get_final_reviewer(), "gemini")

                # Switch model
                engine.set_gemini_reviewer_model("gemini-1.5-pro")
                self.assertEqual(engine.get_gemini_reviewer_model(), "gemini-1.5-pro")

                # Available models check
                avail = engine.get_available_gemini_models()
                model_ids = [m["id"] for m in avail]
                self.assertIn("gemini-3.8-flash-high", model_ids)

                # Snapshot check
                snap = engine.snapshot()
                self.assertIn("final_reviewer_config", snap)
                self.assertEqual(snap["final_reviewer_config"]["final_reviewer"], "gemini")
                self.assertEqual(snap["final_reviewer_config"]["gemini_reviewer_model"], "gemini-1.5-pro")

                # Lifecycle stage label check
                lc_gemini = engine._lifecycle("WAITING_GPT_REVIEW", final_reviewer="gemini")
                stage_labels = [s["label"] for s in lc_gemini["stepper_stages"]]
                self.assertIn("Gemini Review", stage_labels)

                lc_gpt = engine._lifecycle("WAITING_GPT_REVIEW", final_reviewer="chatgpt")
                stage_labels_gpt = [s["label"] for s in lc_gpt["stepper_stages"]]
                self.assertIn("GPT Review", stage_labels_gpt)
            finally:
                engine.state.close()

    def test_run_agent_continue_thread_flag(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Path(td) / "projects.json"
            reg.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p", "repo": "u/r"}]}))
            runtime = Path(td) / "runtime"
            settings = Settings(
                control_repo="",
                token="",
                registry_file=reg,
                runtime_dir=runtime,
                workspace_root=Path(td) / "managed",
                poll_interval=5,
                git_transport="ssh",
                agy_bin="agy",
                agent_effort="medium",
                agent_timeout=1800,
                test_timeout=600,
                lease_timeout=90,
                dashboard_host="127.0.0.1",
                dashboard_port=8766,
                allowed_authors=("u",),
            )
            engine = OrchestratorEngine(settings)
            try:
                captured_args = []
                def fake_run(args, **kwargs):
                    captured_args.extend(args)
                    return 0, "mock output", False

                engine._run_with_heartbeat = fake_run

                which_patch = patch("orchestrator.engine.shutil.which", return_value="/mock/agy")
                which_patch.start()
                self.addCleanup(which_patch.stop)

                agent = {
                    "id": "reviewer",
                    "agy_agent": "05_reviewer",
                    "effort": "high",
                }

                # When continue_thread is False: no --continue
                code, out = engine.run_agent(agent, "review prompt", Path(td), 10, continue_thread=False)
                self.assertEqual(code, 0)
                self.assertNotIn("--continue", captured_args)

                # When continue_thread is True: contains --continue
                captured_args.clear()
                code2, out2 = engine.run_agent(agent, "review prompt 2", Path(td), 10, continue_thread=True)
                self.assertEqual(code2, 0)
                self.assertIn("--continue", captured_args)
            finally:
                engine.state.close()

    def test_dashboard_api_endpoints(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Path(td) / "projects.json"
            reg.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p", "repo": "u/r"}]}))
            runtime = Path(td) / "runtime"
            settings = Settings(
                control_repo="",
                token="",
                registry_file=reg,
                runtime_dir=runtime,
                workspace_root=Path(td) / "managed",
                poll_interval=5,
                git_transport="ssh",
                agy_bin="agy",
                agent_effort="medium",
                agent_timeout=1800,
                test_timeout=600,
                lease_timeout=90,
                dashboard_host="127.0.0.1",
                dashboard_port=0,
                allowed_authors=("u",),
                final_reviewer="chatgpt",
                gemini_reviewer_model="gemini-3.8-flash-high",
            )
            engine = OrchestratorEngine(settings)
            server = make_server(engine, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]

                # GET endpoint
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/config/final-reviewer")
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(data.get("ok"))
                    self.assertEqual(data.get("final_reviewer"), "chatgpt")
                    self.assertEqual(data.get("gemini_reviewer_model"), "gemini-3.8-flash-high")
                    self.assertTrue(isinstance(data.get("available_gemini_models"), list))

                # POST endpoint - update to gemini
                update_body = json.dumps({
                    "final_reviewer": "gemini",
                    "gemini_reviewer_model": "gemini-3.8-flash-high",
                }).encode("utf-8")
                post_req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/config/final-reviewer",
                    data=update_body,
                    headers={"Content-Type": "application/json", "X-Orchestrator-UI": "1"},
                    method="POST",
                )
                with urllib.request.urlopen(post_req) as post_resp:
                    post_data = json.loads(post_resp.read().decode("utf-8"))
                    self.assertTrue(post_data.get("ok"))
                    self.assertEqual(post_data.get("final_reviewer"), "gemini")

                # Verify engine state was updated
                self.assertEqual(engine.get_final_reviewer(), "gemini")
            finally:
                server.shutdown()
                server.server_close()
                engine.state.close()

    def test_gemini_approved_comment_formatting(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Path(td) / "projects.json"
            reg.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p", "repo": "u/r"}]}))
            runtime = Path(td) / "runtime"
            settings = Settings(
                control_repo="",
                token="",
                registry_file=reg,
                runtime_dir=runtime,
                workspace_root=Path(td) / "managed",
                poll_interval=5,
                git_transport="ssh",
                agy_bin="agy",
                agent_effort="medium",
                agent_timeout=1800,
                test_timeout=600,
                lease_timeout=90,
                dashboard_host="127.0.0.1",
                dashboard_port=0,
                allowed_authors=("u",),
                final_reviewer="gemini",
            )
            engine = OrchestratorEngine(settings)
            try:
                engine.state.force_claim(
                    engine.instance_id,
                    7,
                    "RUNNING",
                    {"logical_issue_id": "issue7", "issue_repo": "u/r"},
                )
                marker, body, comment = engine.format_event_comment(
                    7,
                    "gemini_approved",
                    logical_issue_id="issue7",
                    pr_number=12,
                    gemini_model="gemini-3.8-flash-high",
                    issue_repo="u/r",
                )
                self.assertIn("[issue7_approved_byGEMINI]", comment)
                self.assertIn("GEMINI", comment)
                self.assertIn("12", comment)
            finally:
                engine.state.close()

    def test_handle_active_auto_approves_in_gemini_mode(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Path(td) / "projects.json"
            reg.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p", "repo": "u/r"}]}))
            runtime = Path(td) / "runtime"
            settings = Settings(
                control_repo="",
                token="",
                registry_file=reg,
                runtime_dir=runtime,
                workspace_root=Path(td) / "managed",
                poll_interval=5,
                git_transport="ssh",
                agy_bin="agy",
                agent_effort="medium",
                agent_timeout=1800,
                test_timeout=600,
                lease_timeout=90,
                dashboard_host="127.0.0.1",
                dashboard_port=0,
                allowed_authors=("u",),
                final_reviewer="gemini",
            )
            engine = OrchestratorEngine(settings)
            try:
                emitted_events = []
                orig_event = engine.event
                engine.event = lambda issue, event_type, **kw: (emitted_events.append((event_type, kw)), orig_event(issue, event_type, **kw))[1]
                engine.set_label = lambda issue, label: None
                engine.close_issue = lambda issue: None
                engine.telegram.enabled = False
                engine.github.comment = lambda repo, issue, body: {"id": 123}
                engine.github.close_issue = lambda repo, issue: {"state": "closed"}
                engine.github.set_lifecycle_label = lambda repo, issue, label: None
                engine._load_issue_task = lambda num: (
                    {"number": num, "labels": ["orch:gpt-review"], "state": "open"},
                    {"task_id": "issue15", "issue_id": "issue15"},
                    "digest123"
                )
                engine._reconcile_contract = lambda l, i, t, d: (l["payload"], True)
                engine.github.comments = lambda repo, num: []
                engine.github.get_pr = lambda repo, num: {"number": num, "merged": False, "state": "open", "head": {"sha": "abc"}}
                engine.github.merge_pr = lambda repo, pr_num, **kwargs: {"merged": True}
                engine.github.delete_branch = lambda repo, branch: True
                engine.workspace.remove_worktree = lambda repo, branch: None

                # Setup lease in WAITING_GPT_REVIEW
                engine.state.force_claim(
                    engine.instance_id,
                    15,
                    "WAITING_GPT_REVIEW",
                    {
                        "logical_issue_id": "issue15",
                        "issue_repo": "u/r",
                        "target_repo": "u/r",
                        "pr_number": 101,
                        "pr_head_sha": "abc",
                        "branch": "task/issue-15-test",
                        "revision": 1,
                    },
                )
                lease = engine.state.get_lease(15)

                # Execute _handle_active in gemini mode
                engine._handle_active(lease)

                # Should have emitted gemini_approved and complete with post_merge_audit_required
                event_types = [e[0] for e in emitted_events]
                self.assertIn("gemini_approved", event_types)
                self.assertIn("complete", event_types)
                complete_payload = [e[1] for e in emitted_events if e[0] == "complete"][0]
                self.assertTrue(complete_payload.get("post_merge_audit_required"))
            finally:
                engine.state.close()

    def test_post_merge_audit_comment_and_telegram_formatting(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Path(td) / "projects.json"
            reg.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p", "repo": "u/r"}]}))
            runtime = Path(td) / "runtime"
            settings = Settings(
                control_repo="",
                token="",
                registry_file=reg,
                runtime_dir=runtime,
                workspace_root=Path(td) / "managed",
                poll_interval=5,
                git_transport="ssh",
                agy_bin="agy",
                agent_effort="medium",
                agent_timeout=1800,
                test_timeout=600,
                lease_timeout=90,
                dashboard_host="127.0.0.1",
                dashboard_port=0,
                allowed_authors=("u",),
                final_reviewer="gemini",
            )
            engine = OrchestratorEngine(settings)
            try:
                engine.state.force_claim(
                    engine.instance_id,
                    20,
                    "DONE",
                    {"logical_issue_id": "issue20", "issue_repo": "u/r"},
                )
                marker, body, comment = engine.format_event_comment(
                    20,
                    "complete",
                    logical_issue_id="issue20",
                    pr_number=55,
                    issue_repo="u/r",
                    reviewer="Gemini",
                    post_merge_audit_required=True,
                )
                self.assertIn("[issue20_done_byORCH]", comment)
                self.assertIn("Post-Merge Audit Trigger for ChatGPT", comment)
                self.assertIn("condition: [\"issue20\"]", comment)

                # Check Telegram message format
                tg_msg = engine.telegram.format_event_message("complete", {
                    "issue_number": 20,
                    "task_id": "issue20",
                    "title": "Build Player Controller",
                    "issue_repo": "u/r",
                    "pr_number": 55,
                    "reviewer": "Gemini",
                    "post_merge_audit_required": True,
                })
                self.assertIn("HOÀN THÀNH VÀ MERGE PR THÀNH CÔNG", tg_msg)
                self.assertIn("Post-Merge Trigger", tg_msg)
                self.assertIn("ChatGPT", tg_msg)
            finally:
                engine.state.close()


if __name__ == "__main__":
    unittest.main()


