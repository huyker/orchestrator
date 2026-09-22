import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.engine import OrchestratorEngine
from orchestrator.models import Settings
from orchestrator.telegram import TelegramNotifier


class TestTelegramNotifier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_file = Path(self.tmp.name) / "telegram_config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load_config(self):
        notifier = TelegramNotifier(config_file=self.config_file)
        self.assertFalse(notifier.enabled)
        self.assertEqual(notifier.bot_token, "")

        # Save config
        saved = notifier.save_config(
            bot_token="123456:ABC-DEF",
            chat_id="-100987654321",
            enabled=True,
            topic_id="42",
        )
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["chat_id"], "-100987654321")
        self.assertEqual(saved["topic_id"], "42")
        self.assertTrue(self.config_file.is_file())

        # Load into new instance
        new_notifier = TelegramNotifier(config_file=self.config_file)
        self.assertTrue(new_notifier.enabled)
        self.assertEqual(new_notifier.bot_token, "123456:ABC-DEF")
        self.assertEqual(new_notifier.chat_id, "-100987654321")
        self.assertEqual(new_notifier.topic_id, "42")

    def test_format_events(self):
        notifier = TelegramNotifier(config_file=self.config_file)

        # 1. Tiếp nhận xử lý task mới
        msg_started = notifier.format_event_message("started", {
            "issue_number": 6,
            "issue_repo": "huyker/game",
            "task_id": "GAME-GPV1-DOCS-0002",
            "title": "Lock Gameplay Whitepaper V1",
            "branch": "task/issue-6",
            "executor": "executor",
        })
        self.assertIn("TIẾP NHẬN XỬ LÝ TASK MỚI", msg_started)
        self.assertIn("#6", msg_started)
        self.assertIn("GAME-GPV1-DOCS-0002", msg_started)
        self.assertIn("Lock Gameplay Whitepaper V1", msg_started)

        # 2. Đã gửi task đổi trạng thái lên issue cho GPT kiểm tra
        msg_gpt = notifier.format_event_message("ready_for_gpt_review", {
            "issue_number": 4,
            "issue_repo": "huyker/game",
            "task_id": "GAME-GPV1-DEMO-0004",
            "title": "Build HTML/JS demo",
            "pr_url": "https://github.com/huyker/game/pull/10",
            "pr_number": 10,
            "branch": "task/issue-4",
        })
        self.assertIn("ĐÃ GỬI TASK CHO GPT KIỂM TRA (GPT REVIEW)", msg_gpt)
        self.assertIn("PR #10", msg_gpt)
        self.assertIn("orch:gpt-review", msg_gpt)

        # 3. Blocked
        msg_blocked = notifier.format_event_message("blocked", {
            "issue_number": 6,
            "issue_repo": "huyker/game",
            "task_id": "GAME-GPV1-DOCS-0002",
            "reason": "Eligibility check failed: 503",
        })
        self.assertIn("BLOCKED", msg_blocked)
        self.assertIn("503", msg_blocked)

        # 4. Rework
        msg_rework = notifier.format_event_message("rework", {
            "issue_number": 6,
            "issue_repo": "huyker/game",
            "reason": "Fix review feedback",
        })
        self.assertIn("REWORK", msg_rework)

        # 5. Retry
        msg_retry = notifier.format_event_message("retry_requested", {
            "issue_number": 6,
            "issue_repo": "huyker/game",
        })
        self.assertIn("THỬ LẠI TASK", msg_retry)

    @patch("urllib.request.urlopen")
    def test_send_message_and_test_connection(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        notifier = TelegramNotifier(config_file=self.config_file)
        notifier.save_config(bot_token="token123", chat_id="chat123", enabled=True)

        ok, err = notifier.send_message("Hello Test")
        self.assertTrue(ok)

        # Test connection with explicit credentials
        ok, msg = notifier.test_connection(bot_token="tokenABC", chat_id="chatXYZ")
        self.assertTrue(ok)
        self.assertIn("thành công", msg.lower())


class TestTelegramEngineIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        reg_file = root / "registry.json"
        reg_file.write_text(json.dumps({"schema_version": 1, "projects": []}), encoding="utf-8")
        self.tg_file = root / "telegram_config.json"
        self.settings = Settings(
            control_repo="huyker/control",
            token="ghp_test",
            registry_file=reg_file,
            runtime_dir=root / "runtime",
            workspace_root=root / "workspaces",
            poll_interval=1,
            git_transport="ssh",
            agy_bin="agy",
            agent_effort="medium",
            agent_timeout=30,
            test_timeout=2,
            lease_timeout=10,
            dashboard_host="127.0.0.1",
            dashboard_port=0,
            allowed_authors=("test-user",),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_engine_telegram_snapshot_and_save(self):
        with OrchestratorEngine(self.settings, telegram_config=self.tg_file) as engine:
            snap = engine.snapshot()
            self.assertIn("telegram", snap)
            self.assertFalse(snap["telegram"]["enabled"])

            # Save telegram config through engine
            cfg = engine.save_telegram_config(
                bot_token="bot_token_engine",
                chat_id="-10011223344",
                enabled=True,
            )
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["chat_id"], "-10011223344")

            # Verify snapshot reflects update
            snap2 = engine.snapshot()
            self.assertTrue(snap2["telegram"]["enabled"])
            self.assertEqual(snap2["telegram"]["chat_id"], "-10011223344")

    @patch("orchestrator.telegram.TelegramNotifier.send_event_async")
    def test_engine_event_triggers_telegram(self, mock_send_async):
        with OrchestratorEngine(self.settings, telegram_config=self.tg_file) as engine:
            # Fake verified dashboard so event works
            engine.state.mark_dashboard_verified("http://127.0.0.1:8766")
            engine.telegram.enabled = True

            # Trigger event
            engine.event(
                6,
                "started",
                post_to_github=False,
                issue_repo="huyker/game",
                task_id="GAME-TASK-1",
                title="Implement Feature",
            )

            mock_send_async.assert_called_once()
            args = mock_send_async.call_args[0]
            self.assertEqual(args[0], "started")
            self.assertEqual(args[1]["issue_number"], 6)
            self.assertEqual(args[1]["task_id"], "GAME-TASK-1")


class TestDashboardTelegramEndpoints(unittest.TestCase):
    def setUp(self):
        from orchestrator.dashboard import make_server
        import threading

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        reg_file = root / "registry.json"
        reg_file.write_text(json.dumps({"schema_version": 1, "projects": []}), encoding="utf-8")
        self.tg_file = root / "telegram_config.json"
        self.settings = Settings(
            control_repo="huyker/control",
            token="ghp_test",
            registry_file=reg_file,
            runtime_dir=root / "runtime",
            workspace_root=root / "workspaces",
            poll_interval=1,
            git_transport="ssh",
            agy_bin="agy",
            agent_effort="medium",
            agent_timeout=30,
            test_timeout=2,
            lease_timeout=10,
            dashboard_host="127.0.0.1",
            dashboard_port=0,
            allowed_authors=("test-user",),
        )
        self.engine = OrchestratorEngine(self.settings, telegram_config=self.tg_file)
        self.server = make_server(self.engine, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.engine.close()
        self.tmp.cleanup()

    def test_get_and_post_telegram_config(self):
        import urllib.request

        # GET config
        with urllib.request.urlopen(f"{self.base_url}/api/telegram/config") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertIn("config", data)
            self.assertFalse(data["config"]["enabled"])

        # POST config
        req_payload = {
            "bot_token": "token_endpoint_test",
            "chat_id": "-100123456789",
            "topic_id": "99",
            "enabled": True,
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/telegram/config",
            data=json.dumps(req_payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Orchestrator-UI": "1"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(res["ok"])
            self.assertTrue(res["config"]["enabled"])
            self.assertEqual(res["config"]["chat_id"], "-100123456789")
            self.assertEqual(res["config"]["topic_id"], "99")

        # GET config again to verify persisted
        with urllib.request.urlopen(f"{self.base_url}/api/telegram/config") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["config"]["enabled"])
            self.assertEqual(data["config"]["chat_id"], "-100123456789")


if __name__ == "__main__":
    unittest.main()
