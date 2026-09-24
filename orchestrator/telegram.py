from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("orchestrator.telegram")


class TelegramNotifier:
    """Telegram notification service for Orchestrator events, task updates, and GPT review dispatches."""

    def __init__(
        self,
        config_file: Path | str = "telegram_config.json",
        *,
        bot_token: str = "",
        chat_id: str = "",
        enabled: bool = False,
        topic_id: str = "",
    ):
        self.config_file = Path(config_file).resolve()
        self._lock = threading.RLock()
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.topic_id = topic_id
        self.load_config()

    def load_config(self) -> dict[str, Any]:
        with self._lock:
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            topic_id = os.getenv("TELEGRAM_TOPIC_ID", "").strip()
            enabled = os.getenv("TELEGRAM_ENABLED", "0").strip() in ("1", "true", "True", "yes")

            if self.config_file.is_file():
                try:
                    data = json.loads(self.config_file.read_text(encoding="utf-8"))
                    token = str(data.get("bot_token") or token).strip()
                    chat_id = str(data.get("chat_id") or chat_id).strip()
                    topic_id = str(data.get("topic_id") or topic_id).strip()
                    if "enabled" in data:
                        enabled = bool(data["enabled"])
                except Exception as exc:
                    logger.warning("Failed reading %s: %s", self.config_file, exc)

            self.bot_token = token
            self.chat_id = chat_id
            self.topic_id = topic_id
            self.enabled = enabled
            return self.get_config()

    def get_config(self, masked: bool = False) -> dict[str, Any]:
        with self._lock:
            token = self.bot_token
            if masked and token:
                if len(token) > 10:
                    token = f"{token[:6]}...{token[-4:]}"
                else:
                    token = "********"
            return {
                "bot_token": token,
                "chat_id": self.chat_id,
                "topic_id": self.topic_id,
                "enabled": self.enabled,
                "configured": bool(self.bot_token and self.chat_id),
            }

    def save_config(
        self,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
        enabled: bool | None = None,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if bot_token is not None:
                self.bot_token = str(bot_token).strip()
            if chat_id is not None:
                self.chat_id = str(chat_id).strip()
            if enabled is not None:
                self.enabled = bool(enabled)
            if topic_id is not None:
                self.topic_id = str(topic_id).strip()

            payload = {
                "schema_version": 1,
                "enabled": self.enabled,
                "bot_token": self.bot_token,
                "chat_id": self.chat_id,
                "topic_id": self.topic_id,
            }
            self.config_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

            # Persist into .env for fallback
            try:
                env_file = Path(".env")
                lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.is_file() else []
                keys = {
                    "TELEGRAM_BOT_TOKEN": self.bot_token,
                    "TELEGRAM_CHAT_ID": self.chat_id,
                    "TELEGRAM_TOPIC_ID": self.topic_id,
                    "TELEGRAM_ENABLED": "1" if self.enabled else "0",
                }
                new_lines = []
                seen = set()
                for line in lines:
                    k = line.split("=", 1)[0].strip() if "=" in line else ""
                    if k in keys:
                        new_lines.append(f"{k}={keys[k]}")
                        seen.add(k)
                    else:
                        new_lines.append(line)
                for k, v in keys.items():
                    if k not in seen:
                        new_lines.append(f"{k}={v}")
                env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            except Exception as exc:
                logger.warning("Failed updating .env with telegram config: %s", exc)

            return self.get_config()

    def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
        is_test: bool = False,
    ) -> tuple[bool, str]:
        with self._lock:
            token = (bot_token if bot_token is not None else self.bot_token).strip()
            cid = (chat_id if chat_id is not None else self.chat_id).strip()
            tid = (topic_id if topic_id is not None else self.topic_id).strip()
            enabled = self.enabled

        if not is_test and not enabled:
            return False, "Telegram notifications are disabled in settings"
        if not token or not cid:
            return False, "Telegram bot_token or chat_id is missing"

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body_dict: dict[str, Any] = {
            "chat_id": cid,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False,
        }
        if tid:
            try:
                body_dict["message_thread_id"] = int(tid)
            except ValueError:
                pass

        req_data = json.dumps(body_dict).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_text = resp.read().decode("utf-8")
                res = json.loads(resp_text)
                if res.get("ok"):
                    return True, "Message sent successfully"
                return False, f"Telegram API error: {res.get('description', resp_text)}"
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            try:
                desc = json.loads(err_body).get("description", err_body)
            except Exception:
                desc = err_body
            return False, f"HTTP {exc.code}: {desc}"
        except Exception as exc:
            return False, f"Connection failed: {exc}"

    def test_connection(
        self,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
    ) -> tuple[bool, str]:
        token = (bot_token if bot_token is not None else self.bot_token).strip()
        cid = (chat_id if chat_id is not None else self.chat_id).strip()
        tid = (topic_id if topic_id is not None else self.topic_id).strip()
        if not token or not cid:
            return False, "Vui lòng nhập đầy đủ Bot Token và Chat ID / Group ID"
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        test_msg = (
            f"🔔 <b>[ORCHESTRATOR] TEST KẾT NỐI TELEGRAM THÀNH CÔNG!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Bot Token và Chat ID hoạt động chính xác.\n"
            f"📢 Kênh thông báo: <code>{cid}</code>\n"
            + (f"🧵 Topic ID: <code>{tid}</code>\n" if tid else "")
            + f"⏰ Thời gian: {now}\n\n"
            f"🚀 Orchestrator sẽ tự động thông báo mọi sự kiện: nhận task mới, cập nhật, gửi GPT review, blocked, retry..."
        )
        ok, res_desc = self.send_message(
            test_msg,
            bot_token=token,
            chat_id=cid,
            topic_id=tid,
            is_test=True,
        )
        if ok:
            return True, "Gửi tin nhắn thử nghiệm thành công! Hãy kiểm tra Telegram."
        return False, res_desc

    def send_event_async(self, event_type: str, data: dict[str, Any]) -> None:
        """Asynchronously send telegram notification in a daemon thread."""
        if not self.enabled:
            return
        threading.Thread(target=self._notify_event, args=(event_type, data), daemon=True).start()

    def _notify_event(self, event_type: str, data: dict[str, Any]) -> None:
        msg = self.format_event_message(event_type, data)
        if msg:
            ok, err = self.send_message(msg)
            if not ok:
                logger.warning("Telegram notification failed for %s: %s", event_type, err)

    def format_event_message(self, event_type: str, data: dict[str, Any]) -> str:
        issue_num = data.get("issue_number") or data.get("issue") or ""
        issue_repo = data.get("issue_repo") or data.get("repo") or ""
        task_id = data.get("task_id") or ""
        title = data.get("title") or ""
        branch = data.get("branch") or ""
        executor = data.get("executor") or data.get("agent") or "executor"
        pr_url = data.get("pr_url") or data.get("html_url") or ""
        pr_number = data.get("pr_number") or ""
        reason = data.get("reason") or data.get("blocked_reason") or ""
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        issue_url = f"https://github.com/{issue_repo}/issues/{issue_num}" if (issue_repo and issue_num) else ""
        issue_link_html = f'<a href="{issue_url}">#{issue_num} trên GitHub</a>' if issue_url else f"#{issue_num}"

        # 1. Tiếp nhận xử lý task mới
        if event_type in ("started", "task_started", "task_claimed"):
            return (
                f"🚀 <b>[ORCHESTRATOR] TIẾP NHẬN XỬ LÝ TASK MỚI</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Task:</b> #{issue_num} · <code>{task_id or 'TASK'}</code>\n"
                f"🏷 <b>Tiêu đề:</b> {title or 'Đang xử lý'}\n"
                f"📦 <b>Dự án:</b> <code>{issue_repo}</code>\n"
                f"🤖 <b>Agent:</b> <code>{executor}</code>\n"
                f"🌿 <b>Nhánh:</b> <code>{branch or f'task/issue-{issue_num}'}</code>\n"
                f"📊 <b>Trạng thái:</b> ⚡ <b>RUNNING (Stage 1 Implement)</b>\n"
                f"⏰ <b>Thời gian:</b> {now}\n"
                + (f"🔗 <a href='{issue_url}'>Xem Issue trên GitHub</a>" if issue_url else "")
            )

        # 2. Đã gửi task đổi trạng thái lên issue cho GPT kiểm tra
        if event_type in ("ready_for_gpt_review", "fixdone", "waiting_gpt_review"):
            pr_part = f'<a href="{pr_url}">PR #{pr_number} trên GitHub</a>' if pr_url else f"PR #{pr_number}"
            return (
                f"🤖 <b>[ORCHESTRATOR] ĐÃ GỬI TASK CHO GPT KIỂM TRA (GPT REVIEW)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Task:</b> #{issue_num} · <code>{task_id or 'TASK'}</code>\n"
                f"🏷 <b>Tiêu đề:</b> {title or 'Hoàn thành code & sẵn sàng review'}\n"
                f"📦 <b>Dự án:</b> <code>{issue_repo}</code>\n"
                f"🔀 <b>Pull Request:</b> {pr_part}\n"
                f"🌿 <b>Nhánh:</b> <code>{branch}</code>\n"
                f"💬 <b>Trạng thái:</b> Đã cập nhật nhãn <code>orch:gpt-review</code> trên GitHub Issue. Đang chờ GPT / Reviewer kiểm tra và duyệt mã nguồn.\n"
                f"⏰ <b>Thời gian:</b> {now}\n"
                + (f"🔗 <a href='{issue_url}'>Xem Issue trên GitHub</a>" if issue_url else "")
            )

        # 2b. Gemini đã review & phê duyệt trên 1 luồng AGY
        if event_type in ("gemini_approved", "approved_by_gemini"):
            pr_part = f'<a href="{pr_url}">PR #{pr_number} trên GitHub</a>' if pr_url else f"PR #{pr_number}"
            g_model = data.get("gemini_model") or "Gemini"
            return (
                f"✨ <b>[ORCHESTRATOR] GEMINI ĐÃ PHÊ DUYỆT TRÊN AGY THREAD</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Task:</b> #{issue_num} · <code>{task_id or 'TASK'}</code>\n"
                f"🏷 <b>Tiêu đề:</b> {title or 'Hoàn thành code & đã duyệt'}\n"
                f"📦 <b>Dự án:</b> <code>{issue_repo}</code>\n"
                f"🔀 <b>Pull Request:</b> {pr_part}\n"
                f"🤖 <b>Final Reviewer:</b> <code>{g_model}</code> (100% AGY Thread)\n"
                f"💬 <b>Trạng thái:</b> Đã hoàn thành kiểm tra và duyệt tự động trên 1 luồng AGY. Đang tiến hành merge PR.\n"
                f"⏰ <b>Thời gian:</b> {now}\n"
                + (f"🔗 <a href='{issue_url}'>Xem Issue trên GitHub</a>" if issue_url else "")
            )

        # 3. Task bị nghẽn (BLOCKED)
        if event_type == "blocked":
            return (
                f"⚠️ <b>[ORCHESTRATOR] TASK BỊ NGHẼN (BLOCKED)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Task:</b> #{issue_num} · <code>{task_id or 'TASK'}</code>\n"
                f"🏷 <b>Tiêu đề:</b> {title}\n"
                f"📦 <b>Dự án:</b> <code>{issue_repo}</code>\n"
                f"❌ <b>Lý do nghẽn:</b> <code>{reason or 'Gặp lỗi trong quá trình thực thi'}</code>\n"
                f"💡 <i>Bạn có thể bấm 'Thử Lại Task' trên Dashboard hoặc gửi lệnh retry trên GitHub.</i>\n"
                f"⏰ <b>Thời gian:</b> {now}\n"
                + (f"🔗 <a href='{issue_url}'>Xem Issue trên GitHub</a>" if issue_url else "")
            )

        # 4. Task chuyển sang REWORK
        if event_type == "rework":
            return (
                f"↺ <b>[ORCHESTRATOR] TASK CHUYỂN SANG REWORK</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Task:</b> #{issue_num} · <code>{task_id or 'TASK'}</code>\n"
                f"🏷 <b>Tiêu đề:</b> {title}\n"
                f"📦 <b>Dự án:</b> <code>{issue_repo}</code>\n"
                f"🔄 <b>Lý do:</b> {reason or 'Cần điều chỉnh và cập nhật lại theo yêu cầu review / retry'}\n"
                f"⏰ <b>Thời gian:</b> {now}\n"
                + (f"🔗 <a href='{issue_url}'>Xem Issue trên GitHub</a>" if issue_url else "")
            )

        # 5. Agent đặt câu hỏi (QUESTION)
        if event_type == "question":
            q_msg = data.get("message") or "Agent cần thông tin làm rõ từ bạn"
            return (
                f"❓ <b>[ORCHESTRATOR] AGENT CẦN XÁC NHẬN / ĐẶT CÂU HỎI</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Task:</b> #{issue_num} · <code>{task_id or 'TASK'}</code>\n"
                f"💬 <b>Nội dung:</b> {q_msg}\n"
                f"💡 <i>Hãy bình luận trả lời trực tiếp trên GitHub Issue để Agent tiếp tục công việc.</i>\n"
                f"⏰ <b>Thời gian:</b> {now}\n"
                + (f"🔗 <a href='{issue_url}'>Mở Issue để trả lời</a>" if issue_url else "")
            )

        # 6. Chờ duyệt User Gate
        if event_type == "user_gate_required":
            gate_id = data.get("gate_id") or "concept_review"
            pr_part = f'<a href="{pr_url}">PR #{pr_number}</a>' if pr_url else f"PR #{pr_number}"
            return (
                f"👤 <b>[ORCHESTRATOR] CHỜ DUYỆT USER GATE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Task:</b> #{issue_num} · <code>{task_id or 'TASK'}</code>\n"
                f"🚪 <b>Gate:</b> <code>{gate_id}</code>\n"
                f"🔀 <b>Pull Request concept:</b> {pr_part}\n"
                f"💡 <i>Hãy kiểm tra concept và phê duyệt gate trên GitHub Issue.</i>\n"
                f"⏰ <b>Thời gian:</b> {now}\n"
                + (f"🔗 <a href='{issue_url}'>Xem Issue trên GitHub</a>" if issue_url else "")
            )

        # 7. User Gate được duyệt
        if event_type == "user_gate_approved":
            gate_id = data.get("gate_id") or "gate"
            return (
                f"✅ <b>[ORCHESTRATOR] USER GATE ĐÃ ĐƯỢC DUYỆT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Task:</b> #{issue_num} · <code>{task_id or 'TASK'}</code>\n"
                f"🚪 <b>Gate:</b> <code>{gate_id}</code>\n"
                f"⚡ <i>Agent tiếp tục thực thi các giai đoạn tiếp theo.</i>\n"
                f"⏰ <b>Thời gian:</b> {now}\n"
                + (f"🔗 <a href='{issue_url}'>Xem Issue trên GitHub</a>" if issue_url else "")
            )

        # 8. Hoàn thành Task & Trigger Post-Merge Audit
        if event_type in ("complete", "done"):
            pr_part = f'<a href="{pr_url}">PR #{pr_number}</a>' if pr_url else f"PR #{pr_number}"
            reviewer_used = data.get("reviewer") or ("Gemini" if data.get("gemini_model") else "Reviewer")
            return (
                f"🎉 <b>[ORCHESTRATOR] HOÀN THÀNH VÀ MERGE PR THÀNH CÔNG</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Task:</b> #{issue_num} · <code>{task_id or 'TASK'}</code>\n"
                f"🏷 <b>Tiêu đề:</b> {title}\n"
                f"📦 <b>Dự án:</b> <code>{issue_repo}</code>\n"
                f"🔀 <b>PR đã merge:</b> {pr_part}\n"
                f"🛡️ <b>Reviewer duyệt:</b> <code>{reviewer_used}</code>\n"
                f"🤖 <b>Post-Merge Trigger:</b> Đã kích hoạt yêu cầu ChatGPT kiểm tra & review toàn diện thay đổi (hỗ trợ fix lỗi trên task hoặc tạo issue mới nếu cần).\n"
                f"✨ <i>Mã nguồn đã được tích hợp thành công vào nhánh chính!</i>\n"
                f"⏰ <b>Thời gian:</b> {now}\n"
                + (f"🔗 <a href='{issue_url}'>Xem Issue trên GitHub</a>" if issue_url else "")
            )

        # 9. Kích hoạt Retry
        if event_type in ("retry_requested", "retry_accepted", "retry_requested_from_dashboard"):
            return (
                f"🔄 <b>[ORCHESTRATOR] KÍCH HOẠT THỬ LẠI TASK</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Task:</b> #{issue_num}\n"
                f"📦 <b>Dự án:</b> <code>{issue_repo}</code>\n"
                f"⚡ <i>Task đã được mở khóa và nạp lại vào hàng đợi thực thi.</i>\n"
                f"⏰ <b>Thời gian:</b> {now}\n"
                + (f"🔗 <a href='{issue_url}'>Xem Issue trên GitHub</a>" if issue_url else "")
            )

        # General Action update
        action_name = event_type.replace("_", " ").upper()
        return (
            f"ℹ️ <b>[ORCHESTRATOR] CẬP NHẬT HOẠT ĐỘNG: {action_name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            + (f"📌 <b>Task:</b> #{issue_num}\n" if issue_num else "")
            + (f"📦 <b>Dự án:</b> <code>{issue_repo}</code>\n" if issue_repo else "")
            + f"⏰ <b>Thời gian:</b> {now}\n"
            + (f"🔗 <a href='{issue_url}'>Xem Issue trên GitHub</a>" if issue_url else "")
        )
