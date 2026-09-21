import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.models import Settings
from orchestrator.state import StateStore
from orchestrator.engine import OrchestratorEngine


class AgyModelConfigTests(unittest.TestCase):
    def test_default_settings_agy_model(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Path(td) / "projects.json"
            reg.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p", "repo": "u/r"}]}))
            with patch.dict(os.environ, {
                "ORCH_PROJECT_REGISTRY": str(reg),
                "ORCH_RUNTIME_DIR": str(Path(td) / "runtime"),
                "ORCH_MANAGED_ROOT": str(Path(td) / "managed"),
            }, clear=True):
                settings = Settings.from_env()
                self.assertEqual(settings.agy_model, "gemini-3.8-flash-high")

    def test_env_override_agy_model(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Path(td) / "projects.json"
            reg.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "p", "repo": "u/r"}]}))
            with patch.dict(os.environ, {
                "ORCH_PROJECT_REGISTRY": str(reg),
                "ORCH_RUNTIME_DIR": str(Path(td) / "runtime"),
                "ORCH_MANAGED_ROOT": str(Path(td) / "managed"),
                "ORCH_AGY_MODEL": "gemini-3.8-flash-medium",
            }, clear=True):
                settings = Settings.from_env()
                self.assertEqual(settings.agy_model, "gemini-3.8-flash-medium")

    def test_state_store_config(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.sqlite3"
            state = StateStore(db_path)
            try:
                self.assertIsNone(state.get_config("agy_model"))
                state.set_config("agy_model", "gemini-3.8-flash-high")
                self.assertEqual(state.get_config("agy_model"), "gemini-3.8-flash-high")

                # Update existing
                state.set_config("agy_model", "claude-sonnet-4-6")
                self.assertEqual(state.get_config("agy_model"), "claude-sonnet-4-6")
            finally:
                state.close()

    def test_engine_model_management(self):
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
            )
            engine = OrchestratorEngine(settings)
            try:
                self.assertEqual(engine.get_agy_model(), "gemini-3.8-flash-high")

                # Update model
                engine.set_agy_model("gemini-3.8-flash-low")
                self.assertEqual(engine.get_agy_model(), "gemini-3.8-flash-low")

                # Check available models
                models = engine.get_available_agy_models()
                model_ids = [m["id"] for m in models]
                self.assertIn("gemini-3.8-flash-high", model_ids)
                self.assertIn("claude-sonnet-4-6", model_ids)

                # Check snapshot contains agy_config
                snap = engine.snapshot()
                self.assertIn("agy_config", snap)
                self.assertEqual(snap["agy_config"]["model"], "gemini-3.8-flash-low")
            finally:
                engine.state.close()

    def test_run_agent_passes_configured_model(self):
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
            )
            engine = OrchestratorEngine(settings)
            try:
                captured_args = []
                def fake_run(args, **kwargs):
                    captured_args.extend(args)
                    return 0, "mock output", False

                engine._run_with_heartbeat = fake_run

                agent = {
                    "id": "test-agent",
                    "agy_agent": "03_executor",
                    "effort": "high",
                }
                code, out = engine.run_agent(agent, "test prompt", Path(td), 1)
                self.assertEqual(code, 0)
                self.assertIn("--model", captured_args)
                model_idx = captured_args.index("--model")
                self.assertEqual(captured_args[model_idx + 1], "gemini-3.8-flash-high")
                # When model has embedded effort suffix (-high), --effort should NOT be passed
                self.assertNotIn("--effort", captured_args)

                # Test 2: agent with effort: "medium" and model "gemini-3.8-flash-high"
                captured_args.clear()
                agent2 = {"id": "test2", "agy_agent": "03_executor", "effort": "medium"}
                code2, out2 = engine.run_agent(agent2, "prompt 2", Path(td), 2)
                self.assertEqual(code2, 0)
                self.assertIn("--model", captured_args)
                self.assertEqual(captured_args[captured_args.index("--model") + 1], "gemini-3.8-flash-high")
                self.assertNotIn("--effort", captured_args)

                # Test 3: Claude model should never have --effort
                captured_args.clear()
                agent_claude = {"id": "claude", "agy_agent": "03_executor", "model": "claude-sonnet-4-6", "effort": "medium"}
                code3, out3 = engine.run_agent(agent_claude, "prompt 3", Path(td), 3)
                self.assertEqual(code3, 0)
                self.assertIn("--model", captured_args)
                self.assertEqual(captured_args[captured_args.index("--model") + 1], "claude-sonnet-4-6")
                self.assertNotIn("--effort", captured_args)

                # Test 4: Base model without suffix should receive --effort
                captured_args.clear()
                agent_base = {"id": "base", "agy_agent": "03_executor", "model": "gemini-3.8-flash", "effort": "low"}
                code4, out4 = engine.run_agent(agent_base, "prompt 4", Path(td), 4)
                self.assertEqual(code4, 0)
                self.assertIn("--model", captured_args)
                self.assertEqual(captured_args[captured_args.index("--model") + 1], "gemini-3.8-flash")
                self.assertIn("--effort", captured_args)
                self.assertEqual(captured_args[captured_args.index("--effort") + 1], "low")
            finally:
                engine.state.close()


if __name__ == "__main__":
    unittest.main()
