from __future__ import annotations

import datetime
import fnmatch
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

from .github_client import GitHubClient
from .graphify_adapter import GraphifyAdapter
from .models import (
    EVENT_MARKER,
    LABEL_APPROVED,
    LABEL_BLOCKED,
    LABEL_DONE,
    LABEL_GATE,
    LABEL_GPT_REVIEW,
    LABEL_QUESTION,
    LABEL_READY,
    LABEL_REWORK,
    LABEL_RUNNING,
    LABEL_WAITING_CONDITION,
    REVIEW_MARKER,
    Settings,
    canonical_task_hash,
    iter_commands,
    parse_task,
)
from .project import Registry, default_project_id, github_repo_from_source, load_catalog, resolve_profiles, safe_path
from .state import StateStore
from .telegram import TelegramNotifier
from .workspace import WorkspaceManager


DEFAULT_AGY_MODELS: list[dict[str, Any]] = [
    {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)", "default": True},
    {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)"},
    {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"},
    {"id": "gemini-3.7-flash-high", "name": "Gemini 3.7 Flash (High)"},
    {"id": "gemini-3.7-flash-medium", "name": "Gemini 3.7 Flash (Medium)"},
    {"id": "gemini-3.7-flash-low", "name": "Gemini 3.7 Flash (Low)"},
    {"id": "gemini-3.6-flash-high", "name": "Gemini 3.6 Flash (High)"},
    {"id": "gemini-3.6-flash-medium", "name": "Gemini 3.6 Flash (Medium)"},
    {"id": "gemini-3.6-flash-low", "name": "Gemini 3.6 Flash (Low)"},
    {"id": "gemini-3.1-pro-high", "name": "Gemini 3.1 Pro (High)"},
    {"id": "gemini-3.1-pro-low", "name": "Gemini 3.1 Pro (Low)"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)"},
    {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6 (Thinking)"},
    {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)"},
]


class OrchestratorEngine:
    def __init__(self, settings: Settings, telegram_config: Path | str | None = None):
        self.settings = settings
        self.instance_id = str(uuid.uuid4())
        self.github = GitHubClient(settings.token)
        self.registry = Registry(settings.registry_file)
        self.state = StateStore(settings.runtime_dir / "state.sqlite3")
        self.workspace = WorkspaceManager(settings)
        self.graphify = GraphifyAdapter()
        tg_file = telegram_config or Path("telegram_config.json")
        self.telegram = TelegramNotifier(config_file=tg_file)
        self._tick_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._models_lock = threading.Lock()
        self._available_models: list[dict[str, Any]] = list(DEFAULT_AGY_MODELS)
        self._models_refresh_started = False
        self._project_heads: dict[str, str] = {}
        self._last_sync_at: float | None = None
        self._last_sync_datetime: str | None = None
        try:
            self._last_sync_datetime = self.state.get_config("last_sync_datetime")
            stored_ts = self.state.get_config("last_sync_at")
            self._last_sync_at = float(stored_ts) if stored_ts else None
        except Exception:
            pass
        self._last_sync_results: list[dict[str, Any]] = []

        self._github_auth: dict[str, Any] = {
            "connected": False,
            "login": None,
            "name": None,
            "error": "GitHub authentication not checked yet",
        }
        self._self_update_status: dict[str, Any] = {
            "state": "starting",
            "message": "Self updater not checked yet",
            "checked_at": None,
        }

    def close(self) -> None:
        if hasattr(self, "state") and self.state:
            self.state.close()

    def __del__(self) -> None:
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @property
    def task_logs_dir(self) -> Path:
        p = self.settings.runtime_dir / "task-logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def log_task(self, issue_number: int, stage: str, message: str, **details: Any) -> None:
        try:
            log_file = self.task_logs_dir / f"issue_{int(issue_number)}.log"
            now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            lines = [f"[{now_str}] [{stage.upper()}] {message}"]
            for k, v in details.items():
                if v is not None:
                    if isinstance(v, (dict, list)):
                        lines.append(f"  > {k}: {json.dumps(v, ensure_ascii=False, indent=2)}")
                    else:
                        lines.append(f"  > {k}: {v}")
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    def get_task_log(self, issue_number: int, max_lines: int = 500) -> str:
        log_file = self.task_logs_dir / f"issue_{int(issue_number)}.log"
        if not log_file.exists():
            return ""
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            if len(lines) > max_lines:
                return "\n".join(lines[-max_lines:])
            return text
        except Exception as exc:
            return f"Error reading task log: {exc}"

    def _refresh_agy_models_bg(self) -> None:
        binary = shutil.which(self.settings.agy_bin)
        if not binary:
            return
        try:
            proc = subprocess.run([binary, "models"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
            if proc.returncode == 0 and proc.stdout:
                models_dict = {m["id"]: dict(m) for m in DEFAULT_AGY_MODELS}
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if not line or line.startswith("Fetching") or "\t" not in line:
                        continue
                    parts = line.split("\t", 1)
                    mid = parts[0].strip()
                    mname = parts[1].strip() if len(parts) > 1 else mid
                    if mid not in models_dict:
                        models_dict[mid] = {"id": mid, "name": mname}
                with self._models_lock:
                    self._available_models = list(models_dict.values())
        except Exception:
            pass

    def get_available_agy_models(self) -> list[dict[str, Any]]:
        with self._models_lock:
            if not self._models_refresh_started:
                self._models_refresh_started = True
                threading.Thread(target=self._refresh_agy_models_bg, daemon=True).start()
            return list(self._available_models)

    def get_agy_model(self) -> str:
        return self.state.get_config("agy_model") or getattr(self.settings, "agy_model", "gemini-3.8-flash-high") or "gemini-3.8-flash-high"

    def set_agy_model(self, model: str) -> str:
        clean = str(model or "").strip()
        if not clean:
            raise ValueError("Model identifier cannot be empty")
        self.state.set_config("agy_model", clean)
        self.state.add_event("agy_model_changed", {"model": clean})
        return clean

    def get_agent_model(self, agent_id: str) -> str | None:
        if not agent_id:
            return None
        return self.state.get_config(f"agent_model:{agent_id}")

    def set_agent_model(self, agent_id: str, model: str | None) -> str:
        clean_id = str(agent_id or "").strip()
        if not clean_id:
            raise ValueError("agent_id cannot be empty")
        clean_model = str(model or "").strip()
        if clean_model and clean_model.lower() not in ("default", "inherit", "null", "none"):
            self.state.set_config(f"agent_model:{clean_id}", clean_model)
            self.state.add_event("agent_model_changed", {"agent_id": clean_id, "model": clean_model})
            return clean_model
        else:
            self.state.delete_config(f"agent_model:{clean_id}")
            self.state.add_event("agent_model_changed", {"agent_id": clean_id, "model": "default"})
            return self.get_agy_model()

    def resolve_agent_model(self, agent: dict) -> str:
        agent_id = agent.get("id")
        override = self.get_agent_model(agent_id) if agent_id else None
        return override or agent.get("model") or self.get_agy_model()

    def get_final_reviewer(self) -> str:
        val = self.state.get_config("final_reviewer") or getattr(self.settings, "final_reviewer", "chatgpt") or "chatgpt"
        clean = str(val).strip().lower()
        return "gemini" if clean == "gemini" else "chatgpt"

    def set_final_reviewer(self, reviewer: str) -> str:
        clean = str(reviewer or "").strip().lower()
        if clean not in ("chatgpt", "gemini"):
            raise ValueError("final_reviewer must be 'chatgpt' or 'gemini'")
        self.state.set_config("final_reviewer", clean)
        self.state.add_event("final_reviewer_changed", {"final_reviewer": clean})
        return clean

    def get_gemini_reviewer_model(self) -> str:
        return self.state.get_config("gemini_reviewer_model") or getattr(self.settings, "gemini_reviewer_model", "gemini-3.8-flash-high") or "gemini-3.8-flash-high"

    def set_gemini_reviewer_model(self, model: str) -> str:
        clean = str(model or "").strip()
        if not clean:
            raise ValueError("gemini_reviewer_model cannot be empty")
        self.state.set_config("gemini_reviewer_model", clean)
        self.state.add_event("gemini_reviewer_model_changed", {"model": clean})
        return clean

    def get_available_gemini_models(self) -> list[dict[str, Any]]:
        all_models = self.get_available_agy_models()
        gemini_models = [m for m in all_models if "gemini" in str(m.get("id", "")).lower()]
        return gemini_models or [
            {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)", "default": True},
            {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)"},
            {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"},
            {"id": "gemini-3.1-pro-high", "name": "Gemini 3.1 Pro (High)"},
        ]

    # ---------- GitHub authentication ----------
    def refresh_github_auth(self) -> dict[str, Any]:
        self._github_auth = self.github.auth_status()
        self.state.add_event("github_auth", {
            "connected": self._github_auth.get("connected", False),
            "login": self._github_auth.get("login"),
            "error": self._github_auth.get("error"),
        })
        return dict(self._github_auth)

    def github_auth_status(self) -> dict[str, Any]:
        return dict(self._github_auth)

    def set_self_update_status(self, status: dict[str, Any]) -> None:
        self._self_update_status = dict(status)

    def self_update_status(self) -> dict[str, Any]:
        return dict(self._self_update_status)

    def add_managed_project(self, source: str) -> dict[str, Any]:
        repo, _ = github_repo_from_source(source)
        self.registry = Registry(self.settings.registry_file)

        existing = next(
            (item for item in self.registry.list() if item["repo"] == repo),
            None,
        )
        if existing:
            root = self.workspace.sync_project(
                repo,
                existing.get("default_branch"),
            )
            info = self.workspace.inspect_repo(root)
            return {
                **existing,
                "managed_path": str(root),
                "repo_info": info,
                "already_registered": True,
            }

        root = self.workspace.sync_project(repo, None)
        info = self.workspace.inspect_repo(root)
        branch = str(info.get("branch") or "")
        if not branch or branch == "(detached)":
            branch = self.workspace.remote_default_branch(root)

        manifest_path = ".orchestrator/project.json"
        manifest_file = root / manifest_path
        project_id = default_project_id(repo)
        if manifest_file.is_file():
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest_repo = str(manifest.get("repository") or "").strip()
            if manifest_repo and manifest_repo != repo:
                raise ValueError(
                    f"Project manifest repository {manifest_repo} does not match {repo}"
                )
            project_id = str(manifest.get("project") or project_id).strip()

        entry = self.registry.add_project(
            repo,
            project_id=project_id,
            issues_repo=repo,
            default_branch=branch,
            manifest_path=manifest_path,
        )
        self.state.add_event("project_registered", {
            "project": entry["id"],
            "repo": entry["repo"],
            "managed_path": str(root),
        })
        self._commit_registry_change(f"feat(registry): register project {entry['repo']}")
        return {
            **entry,
            "managed_path": str(root),
            "repo_info": info,
            "already_registered": False,
        }

    def _commit_file_change(self, file_path: Path | str, message: str) -> bool:
        p = Path(file_path).resolve()
        if not p.is_file():
            return False
        repo_dir = p.parent
        try:
            inside = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if inside.returncode != 0 or inside.stdout.strip() != "true":
                return False
            top_proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if top_proc.returncode != 0:
                return False
            git_root = Path(top_proc.stdout.strip())
            try:
                rel_name = str(p.relative_to(git_root))
            except ValueError:
                return False
            subprocess.run(
                ["git", "add", rel_name],
                cwd=git_root,
                capture_output=True,
                timeout=10,
            )
            diff_proc = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=git_root,
                capture_output=True,
                timeout=10,
            )
            if diff_proc.returncode != 0:
                subprocess.run(
                    ["git", "commit", "-m", message],
                    cwd=git_root,
                    capture_output=True,
                    timeout=15,
                )
                subprocess.run(
                    ["git", "push", "origin", "main"],
                    cwd=git_root,
                    capture_output=True,
                    timeout=30,
                )
            return True
        except Exception:
            return False

    def _commit_registry_change(self, message: str) -> None:
        self._commit_file_change(self.settings.registry_file, message)

    def save_telegram_config(
        self,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
        enabled: bool | None = None,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        cfg = self.telegram.save_config(
            bot_token=bot_token,
            chat_id=chat_id,
            enabled=enabled,
            topic_id=topic_id,
        )
        self._commit_file_change(self.telegram.config_file, "chore: update telegram notification settings")
        return cfg

    def test_telegram(
        self,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
    ) -> tuple[bool, str]:
        return self.telegram.test_connection(
            bot_token=bot_token,
            chat_id=chat_id,
            topic_id=topic_id,
        )

    def remove_managed_project(self, project_id: str) -> dict[str, Any]:
        self.registry = Registry(self.settings.registry_file)
        removed = self.registry.remove_project(project_id)
        self.state.add_event("project_removed", {
            "project": removed.get("id"),
            "repo": removed.get("repo"),
        })
        self._commit_registry_change(f"chore(registry): remove project {project_id}")
        return removed

    def sync_single_project(self, project_id: str) -> dict[str, Any]:
        self.registry = Registry(self.settings.registry_file)
        project = self.registry.resolve(project_id)
        repo = project["repo"]
        branch = project.get("default_branch")
        root = self.workspace.sync_project(repo, branch)
        info = self.workspace.inspect_repo(root)
        self.state.add_event("project_synced", {
            "project": project["id"],
            "repo": repo,
            "managed_path": str(root),
        })
        return {
            **project,
            "managed_path": str(root),
            "repo_info": info,
            "synced": True,
        }


    @staticmethod
    def _lifecycle(status: str, labels: list[str] | None = None, final_reviewer: str = "chatgpt") -> dict[str, Any]:

        labels = labels or []
        review_label = "Gemini Review" if str(final_reviewer).lower() == "gemini" else "GPT Review"
        stages = [
            {"id": "READY", "label": "Ready"},
            {"id": "IMPLEMENT", "label": "Implement"},
            {"id": "VALIDATE", "label": "Validate"},
            {"id": "INTERNAL_REVIEW", "label": "QA"},
            {"id": "GPT_REVIEW", "label": review_label},
            {"id": "DONE", "label": "Done"},
        ]
        normalized = str(status or "").upper()
        stage_map = {
            "WAITING_CONDITION": 0,
            "READY": 0,
            "RUNNING": 1,
            "WAITING_ANSWER": 1,
            "WAITING_USER_GATE": 2,
            "REWORK": 1,
            "VALIDATING": 2,
            "INTERNAL_REVIEW": 3,
            "WAITING_GPT_REVIEW": 4,
            "APPROVED_WAITING_MERGE": 4,
            "COMPLETED": 5,
            "DONE": 5,
            "BLOCKED": 1,
        }
        if normalized not in stage_map:
            if "orch:done" in labels:
                normalized = "DONE"
            elif "orch:approved" in labels:
                normalized = "APPROVED_WAITING_MERGE"
            elif "orch:gpt-review" in labels:
                normalized = "WAITING_GPT_REVIEW"
            elif "orch:user-gate" in labels:
                normalized = "WAITING_USER_GATE"
            elif "orch:rework" in labels:
                normalized = "REWORK"
            elif "orch:question" in labels:
                normalized = "WAITING_ANSWER"
            elif "orch:running" in labels:
                normalized = "RUNNING"
            elif "orch:blocked" in labels:
                normalized = "BLOCKED"
            elif "orch:waiting-condition" in labels:
                normalized = "WAITING_CONDITION"
            else:
                normalized = "READY"
        index = stage_map.get(normalized, 0)
        progress = [0, 25, 45, 65, 85, 100][index]
        return {
            "status": normalized,
            "stage_index": index,
            "progress_percent": progress,
            "stepper_stages": stages,
            "terminal": index == len(stages) - 1,
            "is_rework": normalized == "REWORK",
            "is_blocked": normalized == "BLOCKED",
            "is_waiting_condition": normalized == "WAITING_CONDITION",
            "waiting_user": normalized in ("WAITING_ANSWER", "WAITING_USER_GATE"),
        }

    # ---------- communication ----------
    def _issue_repo(self, issue_number: int, explicit: str | None = None) -> str:
        if explicit:
            return explicit
        lease = self.state.get_lease(issue_number) or self.state.get_lease()
        if lease and int(lease["issue_number"]) == int(issue_number):
            repo = str(lease["payload"].get("issue_repo") or "").strip()
            if repo:
                return repo
        if self.settings.control_repo:
            return self.settings.control_repo
        raise RuntimeError("managed-project issue_repo is required; no legacy ORCH_CONTROL_REPO fallback is configured")

    def format_event_comment(
        self,
        issue: int,
        event_type: str,
        *,
        issue_repo: str | None = None,
        logical_issue_id: str | None = None,
        task: dict[str, Any] | None = None,
        **payload: Any,
    ) -> tuple[str, dict[str, Any], str]:
        repo = self._issue_repo(issue, issue_repo)
        logical_issue = logical_issue_id
        if not logical_issue and task:
            logical_issue = task.get("issue_id")
        if not logical_issue:
            lease = self.state.get_lease(issue)
            if not lease:
                l = self.state.get_lease()
                if l and int(l.get("issue_number", 0)) == int(issue):
                    lease = l
            if lease and int(lease.get("issue_number", 0)) == int(issue):
                p = lease.get("payload", {})
                logical_issue = p.get("logical_issue_id") or (p.get("task") or {}).get("issue_id")
        if not logical_issue:
            logical_issue = f"issue{issue}"

        actor = "ORCH"
        spec_event = event_type
        if event_type in ("started", "task_started"):
            actor = "AGY"
            spec_event = "started"
        elif event_type in ("ready_for_gpt_review", "fixdone"):
            actor = "AGY"
            spec_event = "fixdone"
        elif event_type == "question":
            actor = "AGY"
            spec_event = "question"
        elif event_type == "blocked":
            actor = "AGY"
            spec_event = "blocked"
        elif event_type in ("done", "complete"):
            actor = "ORCH"
            spec_event = "done"
        elif event_type in ("gemini_approved", "approved_by_gemini"):
            actor = "GEMINI"
            spec_event = "approved"

        body = {
            "schema_version": 1,
            "schema": "orch.event.v1",
            "event_id": f"{logical_issue}-{spec_event}-{int(time.time() * 1000)}",
            "issue_id": logical_issue,
            "event": spec_event,
            "type": event_type,
            "actor": actor,
            "instance_id": self.instance_id,
            "issue_repo": repo,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **payload,
        }
        header = f"[{logical_issue}_{spec_event}_by{actor}]"

        post_merge_notice = ""
        if spec_event == "done":
            reviewer_name = payload.get("reviewer") or ("Gemini" if self.get_final_reviewer() == "gemini" else "ChatGPT")
            post_merge_notice = (
                f"\n\n### 🛡️ Post-Merge Audit Trigger for ChatGPT\n"
                f"Regardless of which reviewer approved this PR ({reviewer_name}), "
                f"ChatGPT is triggered to audit the entire merged changeset for `{logical_issue}`:\n"
                f"- Review all merged code changes, architecture compliance, edge cases, and potential regressions.\n"
                f"- If defects or fixes are identified, ChatGPT must submit a fix request:\n"
                f"  1. Either post review feedback directly in this Issue thread, OR\n"
                f"  2. Create a new follow-up Issue with `condition: [\"{logical_issue}\"]` so Orchestrator and AGY can resolve it immediately.\n"
            )

        comment_body = f"{header}\n\n```orchestrator-event\n" + json.dumps(body, ensure_ascii=False, indent=2) + f"\n```{post_merge_notice}"
        return header, body, comment_body

    def format_handoff(
        self,
        issue: int,
        task: dict[str, Any] | None = None,
        *,
        logical_issue_id: str | None = None,
        issue_repo: str | None = None,
        **payload: Any,
    ) -> tuple[str, dict[str, Any], str]:
        return self.format_event_comment(
            issue,
            "ready_for_gpt_review",
            issue_repo=issue_repo,
            logical_issue_id=logical_issue_id,
            task=task,
            **payload,
        )

    def event(
        self,
        issue: int,
        event_type: str,
        *,
        issue_repo: str | None = None,
        logical_issue_id: str | None = None,
        task: dict[str, Any] | None = None,
        post_to_github: bool | None = None,
        **payload: Any,
    ) -> int:
        header, body, comment_body = self.format_event_comment(
            issue,
            event_type,
            issue_repo=issue_repo,
            logical_issue_id=logical_issue_id,
            task=task,
            **payload,
        )
        repo = self._issue_repo(issue, issue_repo)
        self.state.add_event(event_type, {"issue_repo": repo, "issue_number": issue, **payload})

        # Telegram notification
        try:
            tg_data = {
                "issue_repo": repo,
                "issue_number": issue,
                "logical_issue_id": body["issue_id"],
                **payload,
            }
            lease = self.state.get_lease(issue) or self.state.get_lease()
            if lease and int(lease.get("issue_number", 0)) == int(issue):
                l_payload = lease.get("payload", {})
                l_task = l_payload.get("task", {})
                if not tg_data.get("task_id"):
                    tg_data["task_id"] = l_task.get("task_id") or l_payload.get("task_id")
                if not tg_data.get("title"):
                    tg_data["title"] = l_task.get("title") or l_payload.get("title")
                if not tg_data.get("branch"):
                    tg_data["branch"] = l_payload.get("branch")
                if not tg_data.get("executor"):
                    tg_data["executor"] = l_payload.get("executor")
            self.telegram.send_event_async(event_type, tg_data)
        except Exception:
            pass

        # Only post comments to GitHub Issue when code is ready/submitted or user interaction is needed.
        # Local execution failures (blocked), internal acceptance/review rework cycles remain local-only.
        github_visible_events = {
            "user_gate_required",
            "ready_for_gpt_review",
            "gemini_approved",
            "fixdone",
            "question",
            "complete",
            "done",
        }
        should_post = post_to_github if post_to_github is not None else (event_type in github_visible_events)
        if not should_post:
            return 0

        created = self.github.comment(
            repo,
            issue,
            comment_body,
        )
        return int(created["id"])

    def set_label(self, issue: int, label: str, issue_repo: str | None = None) -> None:
        self.github.set_lifecycle_label(self._issue_repo(issue, issue_repo), issue, label)

    def ensure_labels(self) -> None:
        if not self.state.is_dashboard_verified():
            raise RuntimeError("Dashboard bootstrap has not been verified; managed-project Issue mutation is disabled")
        if not self._github_auth.get("connected"):
            raise RuntimeError("GitHub is not connected; managed-project Issue mutation is disabled")
        # Orchestrator itself is developed directly through code review/merge.
        # Lifecycle labels belong only to managed-project Issue repositories.
        repos = {project["issues_repo"] for project in self.registry.list()}
        for repo in sorted(repos):
            self.github.ensure_labels(repo)

    def _find_command(
        self,
        comments: list[dict],
        issue_number: int,
        command: str,
        revision: int,
        *,
        after_comment_id: int = 0,
        predicate: Callable[[dict], bool] | None = None,
    ) -> tuple[dict, dict] | None:
        for item in reversed(comments):
            comment_id = int(item.get("id") or 0)
            if comment_id <= after_comment_id or self.state.is_command_consumed(comment_id):
                continue
            login = ((item.get("user") or {}).get("login") or "")
            if login not in self.settings.allowed_authors:
                continue
            for payload in iter_commands(item.get("body") or ""):
                if payload.get("command") != command or int(payload.get("revision", -1)) != revision:
                    continue
                if predicate and not predicate(payload):
                    continue
                if self.state.consume_command(comment_id, issue_number, command):
                    return payload, item
        return None

    # ---------- project/catalog ----------
    def sync_projects(self) -> list[dict[str, Any]]:
        if not self._sync_lock.acquire(blocking=False):
            return list(self._last_sync_results)
        try:
            # Reload the registry on every sync so adding/removing managed
            # projects does not require restarting the all-in-one app.
            self.registry = Registry(self.settings.registry_file)
            result: list[dict[str, Any]] = []
            for project in self.registry.list():
                try:
                    root = self.workspace.sync_project(
                        project["repo"],
                        project.get("default_branch"),
                    )
                    repo_info = self.workspace.inspect_repo(root)
                    head = str(repo_info["head"])
                    catalog = load_catalog(root, project.get("manifest_path", ".orchestrator/project.json"))
                    if catalog["manifest"].get("project") != project["id"]:
                        raise ValueError("manifest project id mismatch")
                    if catalog["manifest"].get("repository") != project["repo"]:
                        raise ValueError("manifest repository mismatch")

                    graph_status = self.graphify.status(root, catalog["manifest"])
                    graph_cfg = self.graphify.config(catalog["manifest"])
                    project_changed = self._project_heads.get(project["id"]) != head
                    graph_needs_build = graph_cfg["enabled"] and not graph_status.get("graph_exists")
                    auto_graph = graph_cfg["enabled"] and graph_cfg["auto_update"] not in (False, "off", "never")
                    if auto_graph and (project_changed or graph_needs_build):
                        graph_status = self.graphify.ensure_graph(root, catalog["manifest"])

                    self._project_heads[project["id"]] = head
                    result.append({
                        "id": project["id"],
                        "repo": project["repo"],
                        "ok": True,
                        "head": head,
                        "changed": project_changed,
                        "source": "managed-root",
                        "managed_path": str(root),
                        "repo_info": repo_info,
                        "agents": sorted(catalog["agents"]),
                        "task_profiles": sorted(catalog["tasks"]),
                        "plans": catalog["plans"],
                        "graphify": graph_status,
                    })
                except Exception as exc:
                    result.append({"id": project["id"], "repo": project["repo"], "ok": False, "error": str(exc)})
            now = time.time()
            now_dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._last_sync_at = now
            self._last_sync_datetime = now_dt
            try:
                self.state.set_config("last_sync_at", str(now))
                self.state.set_config("last_sync_datetime", now_dt)
            except Exception:
                pass
            for r in result:
                r["last_sync_at"] = now
                r["last_sync_datetime"] = now_dt
            self._last_sync_results = result
            self.state.add_event("projects_synced", {"projects": result, "sync_datetime": now_dt})
            return result
        finally:
            self._sync_lock.release()


    def reconcile_startup_state(self) -> dict[str, Any]:
        """On startup, query Git and GitHub to synchronize all open issues, adopt existing leases,
        and reconstruct/resume interrupted tasks so processing continues seamlessly."""
        summary: dict[str, Any] = {
            "adopted_leases": 0,
            "reconstructed_leases": 0,
            "released_leases": 0,
            "open_issues_inspected": 0,
            "projects_synced": 0,
            "errors": [],
        }

        # Step 1: Immediately adopt any existing leases from previous process
        try:
            adopted = self.state.adopt_leases_on_startup(self.instance_id)
            summary["adopted_leases"] = len(adopted)
        except Exception as exc:
            summary["errors"].append(f"adopt_leases_error: {exc}")

        # Step 2: Check Git and GitHub for registered projects
        try:
            projects = self.registry.list()
            summary["projects_synced"] = len(projects)
        except Exception as exc:
            summary["errors"].append(f"registry_error: {exc}")
            return summary

        sources: dict[str, list[dict[str, Any]]] = {}
        for p in projects:
            sources.setdefault(p["issues_repo"], []).append(p)

        for issue_repo, projs in sources.items():
            try:
                open_issues = self.github.list_open_orchestrator_issues(issue_repo)
            except Exception as exc:
                summary["errors"].append(f"github_list_issues_error ({issue_repo}): {exc}")
                continue

            summary["open_issues_inspected"] += len(open_issues)

            open_mocked = hasattr(self.github, "list_open_orchestrator_issues") and not (
                hasattr(self.github.list_open_orchestrator_issues, "__self__")
                and self.github.list_open_orchestrator_issues.__self__ is self.github
            )
            if open_mocked:
                all_issues = list(open_issues)
            else:
                try:
                    all_issues = self.github.list_all_orchestrator_issues(issue_repo)
                except Exception:
                    all_issues = list(open_issues)

            # Reconcile conditions using all_issues so closed prerequisite tasks count as DONE
            try:
                self._reconcile_conditions_for_issues(issue_repo, all_issues)
            except Exception:
                pass

            # Inspect issues (including closed) to release any leases that were marked done or closed
            issues_to_inspect = list(open_issues)
            known_inspect_nums = {r.get("number") for r in issues_to_inspect if r.get("number")}
            for r in all_issues:
                if r.get("number") and r["number"] not in known_inspect_nums:
                    issues_to_inspect.append(r)

            for row in issues_to_inspect:
                num = int(row.get("number") or 0)
                if not num:
                    continue
                labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in row.get("labels", [])]
                issue_state = str(row.get("state", "open")).lower()

                # If issue was closed or marked done on GitHub, release lease if one exists
                if issue_state == "closed" or LABEL_DONE in labels or "orch:done" in labels:
                    existing = self.state.get_lease(num)
                    if existing:
                        self.state.release(issue_number=num)
                        summary["released_leases"] += 1
                    continue

                # Determine lifecycle from labels
                lc_status = ""
                if LABEL_REWORK in labels:
                    lc_status = "REWORK"
                elif LABEL_RUNNING in labels:
                    lc_status = "REWORK"  # Interrupted run should resume as rework
                elif LABEL_QUESTION in labels:
                    lc_status = "WAITING_ANSWER"
                elif LABEL_GATE in labels:
                    lc_status = "WAITING_USER_GATE"
                elif LABEL_APPROVED in labels:
                    lc_status = "APPROVED_WAITING_MERGE"
                elif LABEL_GPT_REVIEW in labels:
                    lc_status = "WAITING_GPT_REVIEW"

                if not lc_status:
                    continue

                existing = self.state.get_lease(num)
                if existing:
                    cur_status = existing.get("status")
                    cur_payload = dict(existing.get("payload") or {})
                    if LABEL_APPROVED in labels and cur_status != "APPROVED_WAITING_MERGE":
                        self.state.update_lease(self.instance_id, status="APPROVED_WAITING_MERGE", payload=cur_payload, issue_number=num)
                    # If question or user gate was answered/approved while offline, update to REWORK
                    elif cur_status == "WAITING_ANSWER":
                        try:
                            comments = self.github.comments(issue_repo, num)
                            rev = int(cur_payload.get("revision", 1))
                            after = int(cur_payload.get("command_after_comment_id", 0))
                            qid = cur_payload.get("pending_question_id")
                            if self._find_command(comments, num, "answer", rev, after_comment_id=after, predicate=lambda c: c.get("question_id") == qid):
                                cur_payload.pop("pending_question_id", None)
                                self.state.update_lease(self.instance_id, status="REWORK", payload=cur_payload, issue_number=num)
                                self.set_label(num, LABEL_REWORK, issue_repo)
                        except Exception:
                            pass
                    elif cur_status == "WAITING_USER_GATE":
                        try:
                            comments = self.github.comments(issue_repo, num)
                            rev = int(cur_payload.get("revision", 1))
                            after = int(cur_payload.get("command_after_comment_id", 0))
                            gid = cur_payload.get("pending_gate_id")
                            ad = cur_payload.get("pending_gate_digest")
                            sh = cur_payload.get("pending_gate_pr_head_sha")
                            if self._find_command(comments, num, "approve_gate", rev, after_comment_id=after, predicate=lambda c: c.get("gate_id") == gid and c.get("artifact_digest") == ad and c.get("pr_head_sha") == sh):
                                approved = dict(cur_payload.get("approved_gates", {}))
                                approved[gid] = {"artifact_digest": ad, "pr_head_sha": sh}
                                cur_payload["approved_gates"] = approved
                                cur_payload.pop("pending_gate_id", None)
                                cur_payload.pop("pending_gate_digest", None)
                                cur_payload.pop("pending_gate_pr_head_sha", None)
                                self.state.update_lease(self.instance_id, status="REWORK", payload=cur_payload, issue_number=num)
                                self.set_label(num, LABEL_REWORK, issue_repo)
                        except Exception:
                            pass
                    continue

                # If no lease exists in DB, reconstruct it from GitHub issue!
                try:
                    task = parse_task(row.get("body") or "")
                    logical_id = self._parse_canonical_id(row)
                    primary_proj = projs[0]
                    payload = {
                        "project": task.get("project") or primary_proj["id"],
                        "target_repo": primary_proj["repo"],
                        "base_branch": task.get("base_branch") or primary_proj.get("default_branch", "main"),
                        "task_id": task.get("task_id", f"task-{num}"),
                        "logical_issue_id": logical_id,
                        "revision": int(task.get("revision", 1)),
                        "contract_hash": canonical_task_hash(task),
                        "approved_gates": {},
                        "rework_count": 0,
                        "review_cycle": 0,
                        "issue_repo": issue_repo,
                        "reconstructed_on_startup": True,
                    }
                    if lc_status in ("APPROVED_WAITING_MERGE", "WAITING_GPT_REVIEW"):
                        try:
                            comments = self.github.comments(issue_repo, num)
                            for cmd_name in ("external_review", "pr_opened"):
                                found_cmd = self._find_command(comments, num, cmd_name, int(payload["revision"]))
                                if found_cmd and found_cmd[0].get("pr_number"):
                                    payload["pr_number"] = int(found_cmd[0]["pr_number"])
                                    if found_cmd[0].get("pr_head_sha"):
                                        payload["pr_head_sha"] = found_cmd[0]["pr_head_sha"]
                                    break
                        except Exception:
                            pass
                    self.state.force_claim(self.instance_id, num, lc_status, payload)
                    summary["reconstructed_leases"] += 1
                except Exception as exc:
                    summary["errors"].append(f"reconstruct_lease_error (#{num}): {exc}")

        self.state.add_event("startup_git_reconciled", summary)
        return summary


    def project_snapshot(self, issues: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        rows = []
        all_issues = issues or []
        for project in self.registry.list():
            root = self.workspace.repo_dir(project["repo"])
            p_issues = [
                x for x in all_issues
                if (
                    x.get("issue_repo") == project.get("issues_repo")
                    or project["id"] in x.get("project_ids", [])
                    or (x.get("task") or {}).get("project") == project["id"]
                )
            ]
            p_issues_augmented = []
            for x in p_issues:
                xi = dict(x)
                if not xi.get("task_log"):
                    t_num = xi.get("number")
                    if t_num:
                        log_sample = self.get_task_log(t_num, max_lines=15)
                        if log_sample:
                            xi["task_log"] = log_sample
                p_issues_augmented.append(xi)
            p_issues = p_issues_augmented

            ready_count = sum(1 for x in p_issues if x.get("lifecycle", {}).get("status") == "READY")
            in_prog_count = sum(1 for x in p_issues if x.get("lifecycle", {}).get("stage_index") in (1, 2, 3))
            review_count = sum(1 for x in p_issues if x.get("lifecycle", {}).get("stage_index") == 4)
            done_count = sum(1 for x in p_issues if x.get("lifecycle", {}).get("stage_index") == 5)
            blocked_count = sum(1 for x in p_issues if x.get("lifecycle", {}).get("is_blocked"))
            task_metrics = {
                "total": len(p_issues),
                "ready": ready_count,
                "in_progress": in_prog_count,
                "review": review_count,
                "done": done_count,
                "blocked": blocked_count,
            }
            row: dict[str, Any] = {
                "id": project["id"],
                "repo": project["repo"],
                "issues_repo": project["issues_repo"],
                "managed_path": str(root),
                "default_branch": project.get("default_branch", "main"),
                "synced": root.exists(),
                "last_sync_at": self._last_sync_at,
                "last_sync_datetime": getattr(self, "_last_sync_datetime", None),
                "task_metrics": task_metrics,
                "total_tasks": len(p_issues),
                "ready_tasks": ready_count,
                "in_progress_tasks": in_prog_count,
                "review_tasks": review_count,
                "done_tasks": done_count,
                "blocked_tasks": blocked_count,
                "issues": p_issues,
            }
            if root.exists():
                try:
                    catalog = load_catalog(root, project.get("manifest_path", ".orchestrator/project.json"))
                    repo_info = self.workspace.inspect_repo(root)
                    default_model = self.get_agy_model()
                    agents_dict = {}
                    for aid, acfg in catalog["agents"].items():
                        acfg_copy = dict(acfg)
                        override = self.get_agent_model(aid)
                        acfg_copy["model"] = override or default_model
                        acfg_copy["configured_model"] = override or ""
                        acfg_copy["is_model_inherited"] = not bool(override)
                        agents_dict[aid] = acfg_copy

                    task_configs = {}
                    for tid, tcfg in catalog["tasks"].items():
                        tcfg_copy = dict(tcfg)
                        exec_id = tcfg.get("executor_profile")
                        rev_id = tcfg.get("reviewer_profile")
                        exec_model = agents_dict.get(exec_id, {}).get("model", default_model)
                        rev_model = agents_dict.get(rev_id, {}).get("model", default_model)
                        tcfg_copy["executor_model"] = exec_model
                        tcfg_copy["reviewer_model"] = rev_model
                        task_configs[tid] = tcfg_copy

                    row.update({
                        "repo_info": repo_info,
                        "source": "managed-root",
                        "agents": sorted(agents_dict),
                        "agent_profiles": agents_dict,
                        "task_profiles": sorted(task_configs),
                        "task_profile_configs": task_configs,
                        "plans": catalog["plans"][:100],
                        "graphify": self.graphify.status(root, catalog["manifest"]),
                        "import_status": "synced",
                    })
                except Exception as exc:
                    row["error"] = str(exc)
                    row["import_status"] = "error"
            else:
                row["import_status"] = "needs_sync"
            rows.append(row)
        return rows

    # ---------- subprocess ----------
    def _run_with_heartbeat(
        self,
        args: list[str] | str,
        *,
        cwd: Path,
        timeout: int,
        shell: bool = False,
    ) -> tuple[int, str, bool]:
        with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as output_file:
            output_path = Path(output_file.name)
        try:
            kwargs: dict[str, Any] = {
                "cwd": cwd,
                "stdout": open(output_path, "wb"),
                "stderr": subprocess.STDOUT,
                "shell": shell,
            }
            if os.name != "nt":
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(args, **kwargs)
            started = time.time()
            timed_out = False
            while proc.poll() is None:
                self.state.heartbeat(self.instance_id)
                if time.time() - started > timeout:
                    timed_out = True
                    if os.name != "nt":
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                            time.sleep(1)
                            if proc.poll() is None:
                                os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        proc.kill()
                    break
                time.sleep(1)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            # close file handle opened for Popen
            stream = kwargs["stdout"]
            stream.close()
            output = output_path.read_text(encoding="utf-8", errors="replace")
            return proc.returncode if proc.returncode is not None else -9, output, timed_out
        finally:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass

    def run_agent(
        self,
        agent: dict,
        prompt: str,
        worktree: Path,
        issue_number: int,
        continue_thread: bool = False,
    ) -> tuple[int, str]:
        binary = shutil.which(self.settings.agy_bin)
        if not binary:
            return 127, f"AGY executable not found: {self.settings.agy_bin}"
        prompt_dir = self.settings.runtime_dir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompt_dir / f"issue-{issue_number}-{int(time.time() * 1000)}.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        short = f"Read the complete task instructions from {prompt_file} and execute them exactly."
        model = self.resolve_agent_model(agent)
        args = [
            binary,
            "--dangerously-skip-permissions",
        ]
        if continue_thread:
            args.append("--continue")
        args.extend([
            "--model",
            model,
            "--print",
            short,
            "--agent",
            agent.get("agy_agent", "auto"),
        ])
        effort_suffixes = ("-high", "-medium", "-low")
        has_embedded_effort = any(model.lower().endswith(s) for s in effort_suffixes)
        effort_unsupported = model.lower().startswith("claude-")
        if not has_embedded_effort and not effort_unsupported:
            effort = agent.get("effort") or self.settings.agent_effort
            if effort:
                args.extend(["--effort", str(effort)])
        code, output, timed_out = self._run_with_heartbeat(
            args,
            cwd=worktree,
            timeout=int(agent.get("timeout", self.settings.agent_timeout)),
        )
        if timed_out:
            return 124, output + "\nAGY_TIMEOUT"
        return code, output

    # ---------- context / validation ----------
    def build_context(self, root: Path, task: dict, catalog: dict, profile: dict, agent: dict) -> str:
        files = list(catalog["manifest"].get("context_files", []))
        files += list(task.get("plan_refs", []))
        files += list(profile.get("context_files", []))
        files += list(agent.get("context_files", [])) + list(agent.get("config_files", []))
        chunks: list[str] = []
        seen: set[str] = set()
        for rel in files:
            if rel in seen:
                continue
            seen.add(rel)
            path = safe_path(root, rel)
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                chunks.append(f"### {rel}\n{text[:50000]}")
        return "\n\n".join(chunks)

    def graph_context(self, project_root: Path, catalog: dict, task: dict) -> tuple[str, dict[str, Any]]:
        manifest = catalog["manifest"]
        cfg = self.graphify.config(manifest)
        if not cfg["enabled"]:
            return "", self.graphify.status(project_root, manifest)
        status = self.graphify.ensure_graph(project_root, manifest, heartbeat=lambda: self.state.heartbeat(self.instance_id))
        query = f"{task['title']}\n{task['objective']}"
        return self.graphify.query(project_root, manifest, query, heartbeat=lambda: self.state.heartbeat(self.instance_id)), status

    def _run_named_test(self, command: str, worktree: Path, timeout: int) -> dict[str, Any]:
        code, output, timed_out = self._run_with_heartbeat(command, cwd=worktree, timeout=timeout, shell=True)
        return {
            "command": command,
            "exit_code": code,
            "timed_out": timed_out,
            "output": output[-8000:],
        }

    def machine_acceptance(self, task: dict, catalog: dict, profile: dict, worktree: Path) -> dict[str, Any]:
        checks = dict(task.get("checks") or {})
        for key in ("required_paths", "forbidden_paths", "required_diff_globs", "forbidden_diff_globs", "test_profiles"):
            checks[key] = list(profile.get(key, [])) + list(checks.get(key, []))

        protected = list(catalog["manifest"].get("protected_paths", []))
        allow_protected = (
            task.get("type") == "project-config"
            and task.get("allow_protected_paths") is True
            and task.get("authorization") == "project-config"
        )
        if not allow_protected:
            checks["forbidden_diff_globs"] = protected + checks.get("forbidden_diff_globs", [])

        changed = self.workspace.changed_files(worktree, task["base_branch"])
        failures: list[str] = []
        for rel in checks.get("required_paths", []):
            if not safe_path(worktree, rel).exists():
                failures.append(f"missing required path: {rel}")
        for pat in checks.get("forbidden_paths", []):
            if any(fnmatch.fnmatch(path, pat) for path in changed):
                failures.append(f"forbidden path changed: {pat}")
        for pat in checks.get("required_diff_globs", []):
            if not any(fnmatch.fnmatch(path, pat) for path in changed):
                failures.append(f"required diff glob missing: {pat}")
        for pat in checks.get("forbidden_diff_globs", []):
            if any(fnmatch.fnmatch(path, pat) for path in changed):
                failures.append(f"forbidden diff glob changed: {pat}")
        if checks.get("require_substantive_diff", profile.get("require_substantive_diff", True)) and not changed:
            failures.append("no substantive diff")

        tests: dict[str, Any] = {}
        configured = catalog["manifest"].get("test_profiles", {})
        for name in checks.get("test_profiles", []):
            commands = configured.get(name)
            if not commands:
                failures.append(f"unknown test profile: {name}")
                continue
            outputs = []
            for command in commands:
                result = self._run_named_test(command, worktree, int(checks.get("test_timeout", self.settings.test_timeout)))
                outputs.append(result)
                if result["timed_out"]:
                    failures.append(f"test profile {name} timed out: {command}")
                    break
                if result["exit_code"] != 0:
                    failures.append(f"test profile {name} failed: {command}")
                    break
            tests[name] = outputs
        return {"pass": not failures, "changed_files": changed, "failures": failures, "tests": tests}

    def parse_review(self, output: str, task: dict) -> tuple[dict, list[str]]:
        rows = [line for line in output.splitlines() if line.startswith(REVIEW_MARKER)]
        if len(rows) != 1:
            raise ValueError("Reviewer must emit exactly one @@ORCH_REVIEW@@ record")
        review = json.loads(rows[0][len(REVIEW_MARKER) :])
        errors: list[str] = []
        expected_acceptance = list(task.get("acceptance", []))
        expected_prohibited = list(task.get("prohibited", []))
        amap = {row.get("criterion"): row for row in review.get("acceptance", [])}
        pmap = {row.get("rule"): row for row in review.get("prohibited", [])}
        for criterion in expected_acceptance:
            row = amap.get(criterion)
            if not row or row.get("status") != "PASS" or not str(row.get("evidence", "")).strip():
                errors.append(f"acceptance not proven: {criterion}")
        for rule in expected_prohibited:
            row = pmap.get(rule)
            if not row or row.get("status") != "PASS" or not str(row.get("evidence", "")).strip():
                errors.append(f"prohibited rule not proven: {rule}")
        min_score = int((task.get("review") or {}).get("min_score", 100))
        if review.get("verdict") != "PASS" or int(review.get("score", 0)) < min_score:
            errors.append("review verdict/score failed")
        return review, errors

    # ---------- task lifecycle ----------
    def _load_issue_task(self, issue_number: int) -> tuple[dict, dict, str]:
        issue = self.github.get_issue(self._issue_repo(issue_number), issue_number)
        author = ((issue.get("user") or {}).get("login") or "")
        if author not in self.settings.allowed_authors:
            raise PermissionError(f"unauthorized issue author: {author}")
        task = parse_task(issue.get("body") or "")
        return issue, task, canonical_task_hash(task)

    def _reconcile_contract(self, lease: dict, issue: dict, task: dict, digest: str) -> tuple[dict, bool]:
        payload = dict(lease["payload"])
        claimed_revision = int(payload.get("revision", 0))
        current_revision = int(task["revision"])
        claimed_hash = payload.get("contract_hash")
        issue_num = lease["issue_number"]

        if payload.get("task_id") and payload["task_id"] != task["task_id"]:
            payload.update({"task_id": task["task_id"], "revision": current_revision, "contract_hash": digest, "rework_count": 0})
            self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_num)
            self.log_task(issue_num, "CONTRACT_RESYNC", f"Resynced task payload from {payload.get('task_id')} to {task['task_id']}")
            return payload, True

        if current_revision == claimed_revision and digest != claimed_hash:
            self._block(lease, "same-revision Issue contract mutation detected; increment revision")
            return payload, False
        if current_revision < claimed_revision:
            if payload.get("recovered_from_instance"):
                payload.update({"revision": current_revision, "contract_hash": digest, "rework_count": 0})
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_num)
                self.log_task(issue_num, "CONTRACT_RESYNC", f"Resynced recovered lease revision from {claimed_revision} to {current_revision}")
                return payload, True
            self._block(lease, "Issue revision moved backwards")
            return payload, False
        if current_revision > claimed_revision:
            payload.update({"revision": current_revision, "contract_hash": digest, "rework_count": 0})
            self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_num)
            self.set_label(issue_num, LABEL_REWORK)
            self.event(issue_num, "revision_updated", revision=current_revision)
            self.log_task(issue_num, "REVISION_UPDATED", f"Task contract revision updated to {current_revision}")
            return payload, False
        return payload, True

    def _block(self, lease: dict, reason: str, **extra: Any) -> None:
        payload = dict(lease["payload"])
        issue_num = lease["issue_number"]
        comment_id = self.event(issue_num, "blocked", post_to_github=False, reason=reason, **extra)
        if comment_id:
            payload["command_after_comment_id"] = comment_id
        payload["blocked_reason"] = reason
        if "output" in extra:
            payload["blocked_output"] = str(extra["output"])
        elif "error" in extra:
            payload["blocked_output"] = str(extra["error"])
        elif "report" in extra:
            payload["blocked_output"] = json.dumps(extra["report"], ensure_ascii=False, indent=2)
        elif "errors" in extra:
            payload["blocked_output"] = json.dumps(extra["errors"], ensure_ascii=False, indent=2)
        else:
            payload["blocked_output"] = reason
        self.state.update_lease(self.instance_id, status="BLOCKED", payload=payload, issue_number=issue_num)
        self.log_task(issue_num, "BLOCKED", f"Task blocked: {reason}", **extra)

    def _required_gate(self, task: dict, profile: dict, payload: dict) -> dict | None:
        gates = list(task.get("user_gates", []))
        if profile.get("require_user_gate") and not gates:
            raise ValueError("task profile requires at least one user_gates entry in Issue contract")
        approved = payload.get("approved_gates", {})
        for gate in gates:
            if gate.get("required", True) and gate.get("id") not in approved:
                return gate
        return None

    def _question_from_output(self, output: str) -> dict | None:
        for line in output.splitlines():
            if line.startswith(EVENT_MARKER):
                event = json.loads(line[len(EVENT_MARKER) :])
                if event.get("type") == "question":
                    if not event.get("question_id") or not event.get("message"):
                        raise ValueError("Question event requires question_id and message")
                    return event
        return None

    def _discussion(self, issue_number: int) -> tuple[list[dict], str]:
        comments = self.github.comments(self._issue_repo(issue_number), issue_number)
        rendered = "\n\n".join(
            f"[comment:{row.get('id')}] @{((row.get('user') or {}).get('login') or 'unknown')}: {row.get('body') or ''}"
            for row in comments[-100:]
        )
        return comments, rendered

    def _execute_active(self, lease: dict, issue: dict, task: dict, payload: dict) -> None:
        issue_number = lease["issue_number"]
        issue_repo = self._issue_repo(issue_number)
        project = self.registry.resolve(task["project"])
        if task.get("target_repo") and task["target_repo"] != project["repo"]:
            raise ValueError("Issue target_repo does not match registry")
        task["target_repo"] = project["repo"]
        task["base_branch"] = task.get("base_branch") or project.get("default_branch", "main")

        project_root = self.workspace.sync_project(
            task["target_repo"],
            task["base_branch"],
        )
        worktree, branch = self.workspace.prepare_task(
            task["target_repo"],
            task["base_branch"],
            issue_number,
            task["task_id"],
        )
        catalog = load_catalog(worktree, project.get("manifest_path", ".orchestrator/project.json"))
        manifest = catalog["manifest"]
        if manifest.get("project") != task["project"] or manifest.get("repository") != task["target_repo"]:
            raise ValueError("Project manifest identity mismatch")
        profile, executor, reviewer = resolve_profiles(task, catalog)
        executor["model"] = self.resolve_agent_model(executor)
        final_reviewer_mode = self.get_final_reviewer()
        is_gemini_mode = (final_reviewer_mode == "gemini")
        if is_gemini_mode:
            reviewer["model"] = self.get_gemini_reviewer_model()
        else:
            reviewer["model"] = self.resolve_agent_model(reviewer)

        payload.update({
            "project": task["project"],
            "target_repo": task["target_repo"],
            "base_branch": task["base_branch"],
            "branch": branch,
            "worktree": str(worktree),
            "task_profile": profile["id"],
            "executor_profile": executor["id"],
            "reviewer_profile": reviewer["id"],
        })
        self.state.update_lease(self.instance_id, status="RUNNING", payload=payload, issue_number=issue_number)
        self.set_label(issue_number, LABEL_RUNNING)
        self.log_task(issue_number, "START", f"Starting executor run with model {executor.get('model')}, effort {executor.get('effort')}, role {executor.get('role')}")

        comments, discussion = self._discussion(issue_number)
        graph_context = ""
        graph_status: dict[str, Any] = {}
        try:
            graph_context, graph_status = self.graph_context(project_root, catalog, task)
        except Exception as exc:
            if self.graphify.config(manifest)["required"]:
                raise
            graph_status = {**self.graphify.status(project_root, manifest), "warning": str(exc)}

        project_context = self.build_context(worktree, task, catalog, profile, executor)
        approved_gates = payload.get("approved_gates", {})
        pending_gate = self._required_gate(task, profile, payload)
        gate_instruction = ""
        if pending_gate:
            gate_instruction = (
                f"\nUSER GATE {pending_gate['id']} IS NOT APPROVED. Produce only the review/concept artifacts needed for this gate. "
                "Do not proceed to post-approval/detail/production work. Finish the current turn after the gate artifacts are ready.\n"
            )

        prompt = f"""# Local executor task\n\nGitHub Issue: {issue_repo}#{issue_number}\nThe Issue task contract is authoritative. Repository plan/rule files are context only.\n\n## Task contract\n{json.dumps(task, ensure_ascii=False, indent=2)}\n\n## Project task profile\n{json.dumps(profile, ensure_ascii=False, indent=2)}\n\n## Project executor profile\n{json.dumps(executor, ensure_ascii=False, indent=2)}\n\n## Approved user gates\n{json.dumps(approved_gates, ensure_ascii=False, indent=2)}\n{gate_instruction}\n## Project context\n{project_context}\n\n## Graphify context (advisory project intelligence, never task authority)\n{graph_context or '[not available]'}\n\n## Latest Issue discussion\n{discussion}\n\nRules:\n- Implement only this Issue contract and current revision.\n- Do not commit, push, create or merge PRs; orchestrator owns Git lifecycle.\n- Never edit orchestrator/project control files unless this task is explicitly authorized as project-config.\n- If product/user input is required, emit exactly one final line:\n  @@ORCH_EVENT@@ {{\"type\":\"question\",\"question_id\":\"stable-id\",\"message\":\"...\",\"options\":[]}}\n- Otherwise complete the requested implementation and local checks.\n"""
        self.log_task(issue_number, "PROMPT", "Generated prompt for executor", prompt_preview=prompt[:600] + "...")
        agy_thread_started = bool(payload.get("agy_thread_started"))
        code, output = self.run_agent(
            executor,
            prompt,
            worktree,
            issue_number,
            continue_thread=(is_gemini_mode and agy_thread_started),
        )
        payload["agy_thread_started"] = True
        self.log_task(issue_number, "EXECUTOR_OUTPUT", f"Executor finished with exit code {code}", output=output)
        question = self._question_from_output(output)
        if question:
            if is_gemini_mode:
                q_msg = question.get("message", "")
                q_opts = question.get("options", [])
                consult_prompt = (
                    f"# Lead Reviewer/Architect Decision on Question\nGitHub Issue: {issue_repo}#{issue_number}\n\n"
                    f"The executor agent raised a question:\n"
                    f"- Question ID: {question.get('question_id')}\n"
                    f"- Question: {q_msg}\n"
                    f"- Options: {json.dumps(q_opts, ensure_ascii=False)}\n\n"
                    f"As the lead architect and final reviewer ({reviewer.get('model')}), provide the definitive technical guidance "
                    f"so the executor can proceed immediately on this AGY thread.\n"
                )
                self.log_task(issue_number, "QUESTION", f"Executor asked: {question.get('question_id')}. Consulting Gemini on AGY thread...", message=q_msg)
                ans_code, ans_output = self.run_agent(reviewer, consult_prompt, worktree, issue_number, continue_thread=True)
                ans_text = ans_output.strip() if ans_code == 0 and ans_output.strip() else "Proceed according to task specification and conventions."
                self.event(issue_number, "question", **question)
                self.event(issue_number, "answer_received", question_id=question.get("question_id"), answer=ans_text, answered_by="gemini")
                self.log_task(issue_number, "ANSWER", f"Gemini answered on AGY thread: {ans_text[:200]}...")
                payload["last_consult_answer"] = ans_text
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_number)
                self.set_label(issue_number, LABEL_REWORK)
                return
            else:
                comment_id = self.event(issue_number, "question", **question)
                payload.update({
                    "pending_question_id": question["question_id"],
                    "command_after_comment_id": comment_id,
                })
                self.state.update_lease(self.instance_id, status="WAITING_ANSWER", payload=payload, issue_number=issue_number)
                self.set_label(issue_number, LABEL_QUESTION)
                self.log_task(issue_number, "QUESTION", f"Agent emitted question: {question.get('question_id')}", message=question.get('message'))
                return
        if code:
            is_transient = "503" in output or "unavailable" in output.lower() or "temporarily unavailable" in output.lower() or "rate limit" in output.lower()
            attempt = int(payload.get("transient_retry_count", 0))
            if is_transient and attempt < 3:
                payload["transient_retry_count"] = attempt + 1
                last_line = output.strip().splitlines()[-1] if output.strip().splitlines() else "503 UNAVAILABLE"
                self.log_task(issue_number, "TRANSIENT_RETRY", f"Detected transient service error: {last_line}. Auto-retrying (attempt {attempt + 1}/3)...")
                time.sleep(3)
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_number)
                return
            self._block(lease, "executor failed", exit_code=code, output=output[-4000:])
            return

        if pending_gate:
            changed = self.workspace.changed_files(worktree, task["base_branch"])
            if not changed:
                self._block(lease, f"user gate {pending_gate['id']} produced no changed artifacts")
                return
            artifact_digest = self.workspace.artifact_digest(worktree, task["base_branch"])
            gate_head_sha = self.workspace.commit_push(
                worktree,
                branch,
                f"gate({task['task_id']}): {pending_gate['id']} concept for issue #{issue_number}",
            )
            pr = self.github.find_open_pr(task["target_repo"], branch, task["base_branch"])
            if not pr:
                pr = self.github.create_pr(
                    task["target_repo"],
                    f"{task['task_id']}: {task['title']}",
                    (
                        f"Implements {issue_repo}#{issue_number}.\n\n"
                        f"Currently waiting for user gate `{pending_gate['id']}`. "
                        "The Issue remains the approval/source-of-truth thread."
                    ),
                    branch,
                    task["base_branch"],
                )
            comment_id = self.event(
                issue_number,
                "user_gate_required",
                gate_id=pending_gate["id"],
                message=pending_gate.get("message", "User approval required"),
                artifact_digest=artifact_digest,
                changed_files=changed,
                pr_url=pr["html_url"],
                pr_number=pr["number"],
                pr_head_sha=gate_head_sha,
            )
            payload.update({
                "pr_number": pr["number"],
                "pending_gate_id": pending_gate["id"],
                "pending_gate_digest": artifact_digest,
                "pending_gate_pr_head_sha": gate_head_sha,
                "command_after_comment_id": comment_id,
            })
            self.state.update_lease(self.instance_id, status="WAITING_USER_GATE", payload=payload, issue_number=issue_number)
            self.set_label(issue_number, LABEL_GATE)
            self.log_task(issue_number, "USER_GATE_REQUIRED", f"User gate required: {pending_gate['id']}", pr_number=pr['number'], pr_head_sha=gate_head_sha)
            return

        self.state.update_lease(self.instance_id, status="VALIDATING", payload=payload, issue_number=issue_number)
        first = self.machine_acceptance(task, catalog, profile, worktree)
        if not first["pass"]:
            self._schedule_rework(lease, payload, task, "acceptance_failed", report=first)
            return

        diff = self.workspace.diff(worktree, task["base_branch"])
        reviewer_context = self.build_context(worktree, task, catalog, profile, reviewer)
        reviewer_prompt = f"""# Independent reviewer\n\nGitHub Issue: {issue_repo}#{issue_number}\nYou did not implement this task. Review the Issue contract plus actual files/diff/tests.\n\n## Task\n{json.dumps(task, ensure_ascii=False, indent=2)}\n\n## Reviewer profile\n{json.dumps(reviewer, ensure_ascii=False, indent=2)}\n\n## Project context\n{reviewer_context}\n\n## Graphify context (advisory)\n{graph_context or '[not available]'}\n\n## Deterministic acceptance\n{json.dumps(first, ensure_ascii=False, indent=2)}\n\n## Actual diff\n{diff}\n\nInspect actual files/assets/tests directly. End output with exactly one line:\n@@ORCH_REVIEW@@ {{\"verdict\":\"PASS|FAIL\",\"score\":0,\"summary\":\"...\",\"findings\":[],\"acceptance\":[{{\"criterion\":\"exact acceptance string\",\"status\":\"PASS|FAIL\",\"evidence\":\"exact evidence\"}}],\"prohibited\":[{{\"rule\":\"exact prohibited string\",\"status\":\"PASS|FAIL\",\"evidence\":\"exact evidence\"}}],\"risks\":[]}}\nEvery acceptance/prohibited item must appear exactly and include evidence.\n"""
        self.state.update_lease(self.instance_id, status="INTERNAL_REVIEW", payload=payload, issue_number=issue_number)
        self.log_task(issue_number, "REVIEWER_START", f"Starting independent reviewer with model {reviewer.get('model')}")
        reviewer_code, reviewer_output = self.run_agent(
            reviewer,
            reviewer_prompt,
            worktree,
            issue_number,
            continue_thread=(is_gemini_mode and payload.get("agy_thread_started", False)),
        )
        self.log_task(issue_number, "REVIEWER_OUTPUT", f"Reviewer finished with exit code {reviewer_code}", output=reviewer_output)
        if reviewer_code:
            self._block(lease, "reviewer infrastructure failure", exit_code=reviewer_code, output=reviewer_output[-4000:])
            return
        review, review_errors = self.parse_review(reviewer_output, task)
        if review_errors:
            self._schedule_rework(lease, payload, task, "review_failed", review=review, errors=review_errors)
            return

        self.state.update_lease(self.instance_id, status="VALIDATING", payload=payload, issue_number=issue_number)
        final = self.machine_acceptance(task, catalog, profile, worktree)
        if not final["pass"]:
            self._schedule_rework(lease, payload, task, "final_acceptance_failed", report=final)
            return

        head_sha = self.workspace.commit_push(worktree, branch, f"task({task['task_id']}): issue #{issue_number}")
        pr = self.github.find_open_pr(task["target_repo"], branch, task["base_branch"])
        if not pr:
            pr = self.github.create_pr(
                task["target_repo"],
                f"{task['task_id']}: {task['title']}",
                f"Implements {issue_repo}#{issue_number}.\n\nTask/Q&A/status/review contract remains in the Issue.",
                branch,
                task["base_branch"],
            )
        review_cycle = int(payload.get("review_cycle", 0)) + 1
        if is_gemini_mode:
            comment_id = self.event(
                issue_number,
                "gemini_approved",
                logical_issue_id=payload.get("logical_issue_id") or (task.get("issue_id") if task else None),
                task=task,
                pr_url=pr["html_url"],
                pr_number=pr["number"],
                pr_head_sha=head_sha,
                review_cycle=review_cycle,
                review=review,
                gemini_model=self.get_gemini_reviewer_model(),
                acceptance=final,
                graphify=graph_status,
            )
            payload.update({
                "pr_number": pr["number"],
                "pr_head_sha": head_sha,
                "review_cycle": review_cycle,
                "command_after_comment_id": comment_id,
                "rework_count": 0,
            })
            self.state.update_lease(self.instance_id, status="APPROVED_WAITING_MERGE", payload=payload, issue_number=issue_number)
            self.set_label(issue_number, LABEL_APPROVED)
            self.log_task(issue_number, "GEMINI_APPROVED", f"Task approved 100% on AGY thread by Gemini ({self.get_gemini_reviewer_model()}): PR #{pr['number']}, sha {head_sha}")
            try:
                merge_res = self.github.merge_pr(
                    payload["target_repo"],
                    pr["number"],
                    commit_title=f"Merge pull request #{pr['number']} for issue #{issue_number} (Gemini auto-approved)",
                )
                if merge_res.get("merged"):
                    self.event(
                        issue_number,
                        "complete",
                        merged_pr=pr.get("html_url") or f"PR #{pr['number']}",
                        pr_number=pr["number"],
                        pr_head_sha=head_sha,
                        reviewer="gemini",
                        gemini_model=self.get_gemini_reviewer_model(),
                        post_merge_audit_required=True,
                    )
                    self.set_label(issue_number, LABEL_DONE)
                    self.github.close_issue(self._issue_repo(issue_number), issue_number)
                    self.state.release(self.instance_id, issue_number=issue_number)
                    self.log_task(issue_number, "DONE", f"Task completed and PR #{pr['number']} merged autonomously by Gemini reviewer.")
            except Exception as exc:
                logger.warning("Auto-merge PR #%s failed: %s", pr["number"], exc)
            return
        else:
            comment_id = self.event(
                issue_number,
                "ready_for_gpt_review",
                logical_issue_id=payload.get("logical_issue_id") or (task.get("issue_id") if task else None),
                task=task,
                pr_url=pr["html_url"],
                pr_number=pr["number"],
                pr_head_sha=head_sha,
                review_cycle=review_cycle,
                internal_review=review,
                acceptance=final,
                graphify=graph_status,
            )
            payload.update({
                "pr_number": pr["number"],
                "pr_head_sha": head_sha,
                "review_cycle": review_cycle,
                "command_after_comment_id": comment_id,
                "rework_count": 0,
            })
            self.state.update_lease(self.instance_id, status="WAITING_GPT_REVIEW", payload=payload, issue_number=issue_number)
            self.set_label(issue_number, LABEL_GPT_REVIEW)
            self.log_task(issue_number, "READY_FOR_GPT_REVIEW", f"Task is ready for GPT review: PR #{pr['number']}, sha {head_sha}")

    def _schedule_rework(self, lease: dict, payload: dict, task: dict, event_type: str, **details: Any) -> None:
        issue_number = lease["issue_number"]
        count = int(payload.get("rework_count", 0)) + 1
        max_rework = int((task.get("review") or {}).get("max_rework", 5))
        comment_id = self.event(issue_number, event_type, post_to_github=False, rework_count=count, **details)
        payload.update({"rework_count": count})
        if comment_id:
            payload["command_after_comment_id"] = comment_id
        if count > max_rework:
            payload["blocked_reason"] = f"rework limit exceeded ({max_rework})"
            payload["blocked_output"] = f"Task rework count {count} exceeded max_rework limit of {max_rework}."
            self.state.update_lease(self.instance_id, status="BLOCKED", payload=payload, issue_number=issue_number)
            self.log_task(issue_number, "BLOCKED", payload["blocked_reason"], output=payload["blocked_output"])
            return
        self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_number)
        self.set_label(issue_number, LABEL_REWORK)
        self.log_task(issue_number, "REWORK", f"Scheduled rework count {count}/{max_rework}", event=event_type, **details)

    def _handle_active(self, lease: dict) -> None:
        issue_number = lease["issue_number"]
        issue, task, digest = self._load_issue_task(issue_number)
        labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in issue.get("labels", [])]
        issue_state = str(issue.get("state", "open")).lower()

        # If issue is already closed or marked done, release worker lease
        if issue_state == "closed" or LABEL_DONE in labels or "orch:done" in labels:
            self.state.release(self.instance_id, issue_number=issue_number)
            return

        payload, ok = self._reconcile_contract(lease, issue, task, digest)
        if not ok:
            return
        status = lease["status"]

        # Synchronize lifecycle if issue was transitioned on GitHub
        if LABEL_APPROVED in labels and status not in ("APPROVED_WAITING_MERGE", "DONE", "COMPLETED"):
            status = "APPROVED_WAITING_MERGE"
            self.state.update_lease(self.instance_id, status="APPROVED_WAITING_MERGE", payload=payload, issue_number=issue_number)
        elif LABEL_GPT_REVIEW in labels and status not in ("WAITING_GPT_REVIEW", "APPROVED_WAITING_MERGE", "BLOCKED"):
            status = "WAITING_GPT_REVIEW"
            self.state.update_lease(self.instance_id, status="WAITING_GPT_REVIEW", payload=payload, issue_number=issue_number)

        comments = self.github.comments(self._issue_repo(issue_number), issue_number)
        revision = int(payload["revision"])
        after = int(payload.get("command_after_comment_id", 0))

        if status == "RECOVERING":
            self.event(issue_number, "recovered_after_restart", post_to_github=False, previous_instance=payload.get("recovered_from_instance"))
            payload.pop("blocked_reason", None)
            payload.pop("blocked_output", None)
            self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_number)
            self.set_label(issue_number, LABEL_REWORK)
            return

        if status in ("RUNNING", "REWORK", "VALIDATING", "INTERNAL_REVIEW"):
            payload.pop("blocked_reason", None)
            payload.pop("blocked_output", None)
            self._execute_active(lease, issue, task, payload)
            return

        if status == "WAITING_ANSWER":
            question_id = payload.get("pending_question_id")
            found = self._find_command(
                comments,
                issue_number,
                "answer",
                revision,
                after_comment_id=after,
                predicate=lambda cmd: cmd.get("question_id") == question_id,
            )
            if found:
                command, _ = found
                self.event(issue_number, "answer_received", question_id=question_id, answer=command.get("answer"))
                payload.pop("pending_question_id", None)
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_number)
                self.set_label(issue_number, LABEL_REWORK)
            return

        if status == "WAITING_USER_GATE":
            gate_id = payload.get("pending_gate_id")
            artifact_digest = payload.get("pending_gate_digest")
            expected_head = payload.get("pending_gate_pr_head_sha")
            pr_number = payload.get("pr_number")
            if pr_number and expected_head:
                pr = self.github.get_pr(payload["target_repo"], int(pr_number))
                actual_head = ((pr.get("head") or {}).get("sha") or "")
                if actual_head and actual_head != expected_head:
                    self.event(
                        issue_number,
                        "user_gate_artifact_changed",
                        gate_id=gate_id,
                        previous_pr_head_sha=expected_head,
                        current_pr_head_sha=actual_head,
                    )
                    payload.pop("pending_gate_id", None)
                    payload.pop("pending_gate_digest", None)
                    payload.pop("pending_gate_pr_head_sha", None)
                    self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_number)
                    self.set_label(issue_number, LABEL_REWORK)
                    return
            found = self._find_command(
                comments,
                issue_number,
                "approve_gate",
                revision,
                after_comment_id=after,
                predicate=lambda cmd: (
                    cmd.get("gate_id") == gate_id
                    and cmd.get("artifact_digest") == artifact_digest
                    and cmd.get("pr_head_sha") == expected_head
                ),
            )
            if found:
                approved = dict(payload.get("approved_gates", {}))
                approved[gate_id] = {
                    "artifact_digest": artifact_digest,
                    "pr_head_sha": expected_head,
                }
                payload["approved_gates"] = approved
                payload.pop("pending_gate_id", None)
                payload.pop("pending_gate_digest", None)
                payload.pop("pending_gate_pr_head_sha", None)
                self.event(
                    issue_number,
                    "user_gate_approved",
                    gate_id=gate_id,
                    artifact_digest=artifact_digest,
                    pr_head_sha=expected_head,
                )
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_number)
                self.set_label(issue_number, LABEL_REWORK)
            return

        if status == "WAITING_GPT_REVIEW":
            if self.get_final_reviewer() == "gemini":
                self.event(
                    issue_number,
                    "gemini_approved",
                    review_cycle=int(payload.get("review_cycle", 1)),
                    pr_head_sha=payload.get("pr_head_sha"),
                    gemini_model=self.get_gemini_reviewer_model(),
                )
                self.state.update_lease(self.instance_id, status="APPROVED_WAITING_MERGE", payload=payload, issue_number=issue_number)
                self.set_label(issue_number, LABEL_APPROVED)
                status = "APPROVED_WAITING_MERGE"
            elif LABEL_APPROVED in labels:
                self.event(issue_number, "gpt_approved", review_cycle=int(payload.get("review_cycle", 1)), pr_head_sha=payload.get("pr_head_sha"))
                self.state.update_lease(self.instance_id, status="APPROVED_WAITING_MERGE", payload=payload, issue_number=issue_number)
                self.set_label(issue_number, LABEL_APPROVED)
                status = "APPROVED_WAITING_MERGE"
            else:
                pr_number = int(payload.get("pr_number") or 0)
                if pr_number:
                    pr = self.github.get_pr(payload["target_repo"], pr_number)
                    actual_head = ((pr.get("head") or {}).get("sha") or "")
                    if actual_head and actual_head != payload.get("pr_head_sha"):
                        self.event(issue_number, "pr_head_changed", previous=payload.get("pr_head_sha"), current=actual_head)
                        payload["pr_head_sha"] = actual_head
                        self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_number)
                        self.set_label(issue_number, LABEL_REWORK)
                        return
                cycle = int(payload.get("review_cycle", 1))
                expected_sha = payload.get("pr_head_sha")
                found = self._find_command(
                    comments,
                    issue_number,
                    "external_review",
                    revision,
                    after_comment_id=after,
                    predicate=lambda cmd: (
                        (cycle <= 0 or int(cmd.get("review_cycle", -1)) >= cycle)
                        and (not expected_sha or not cmd.get("pr_head_sha") or cmd.get("pr_head_sha") == expected_sha)
                    ),
                )
                if not found:
                    return
                command, _ = found
                if command.get("pr_number") and not payload.get("pr_number"):
                    payload["pr_number"] = int(command["pr_number"])
                if command.get("pr_head_sha") and not payload.get("pr_head_sha"):
                    payload["pr_head_sha"] = command["pr_head_sha"]
                verdict = command.get("verdict")
                if verdict == "FIX_REQUIRED":
                    self.event(issue_number, "gpt_fix_required", findings=command.get("findings", []), review_cycle=cycle)
                    self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_number)
                    self.set_label(issue_number, LABEL_REWORK)
                    return
                elif verdict == "PASS":
                    self.event(issue_number, "gpt_approved", review_cycle=cycle, pr_head_sha=expected_sha or payload.get("pr_head_sha"))
                    self.state.update_lease(self.instance_id, status="APPROVED_WAITING_MERGE", payload=payload, issue_number=issue_number)
                    self.set_label(issue_number, LABEL_APPROVED)
                    status = "APPROVED_WAITING_MERGE"
                else:
                    self._block(lease, f"invalid external_review verdict: {verdict}")
                    return

        if status == "APPROVED_WAITING_MERGE":
            pr_num = int(payload.get("pr_number") or 0)
            if not pr_num:
                for cmd_name in ("external_review", "pr_opened"):
                    found_cmd = self._find_command(comments, issue_number, cmd_name, revision)
                    if found_cmd and found_cmd[0].get("pr_number"):
                        pr_num = int(found_cmd[0]["pr_number"])
                        payload["pr_number"] = pr_num
                        self.state.update_lease(self.instance_id, status=status, payload=payload, issue_number=issue_number)
                        break
            if not pr_num:
                branch = payload.get("branch") or f"task-{issue_number}"
                base = payload.get("base_branch") or "main"
                open_pr = self.github.find_open_pr(payload["target_repo"], branch, base)
                if open_pr and open_pr.get("number"):
                    pr_num = int(open_pr["number"])
                    payload["pr_number"] = pr_num
                    self.state.update_lease(self.instance_id, status=status, payload=payload, issue_number=issue_number)

            if not pr_num:
                self._block(lease, "PR number not found for approved task")
                return

            pr = self.github.get_pr(payload["target_repo"], pr_num)
            if pr.get("merged_at") or pr.get("merged"):
                self.event(
                    issue_number,
                    "complete",
                    merged_pr=pr.get("html_url") or f"PR #{pr_num}",
                    pr_number=pr_num,
                    reviewer=self.get_final_reviewer(),
                    post_merge_audit_required=True,
                )
                self.set_label(issue_number, LABEL_DONE)
                self.github.close_issue(self._issue_repo(issue_number), issue_number)
                self.state.release(self.instance_id, issue_number=issue_number)
                return
            elif pr.get("state") == "open":
                # Auto-merge the approved PR!
                try:
                    merge_res = self.github.merge_pr(
                        payload["target_repo"],
                        pr_num,
                        commit_title=f"Merge pull request #{pr_num} for issue #{issue_number}",
                    )
                    if merge_res.get("merged"):
                        self.event(
                            issue_number,
                            "complete",
                            merged_pr=pr.get("html_url") or f"PR #{pr_num}",
                            pr_number=pr_num,
                            reviewer=self.get_final_reviewer(),
                            post_merge_audit_required=True,
                        )
                        self.set_label(issue_number, LABEL_DONE)
                        self.github.close_issue(self._issue_repo(issue_number), issue_number)
                        self.state.release(self.instance_id, issue_number=issue_number)
                        return
                except Exception as exc:
                    logger.warning("Auto-merge PR #%s failed: %s", pr_num, exc)
            elif pr.get("state") == "closed":
                self._block(lease, "PR closed without merge")
            return

        if status == "BLOCKED":
            found = self._find_command(
                comments,
                issue_number,
                "retry",
                revision,
                after_comment_id=after,
            )
            if found:
                self.event(issue_number, "retry_accepted", post_to_github=False)
                payload.pop("blocked_reason", None)
                payload.pop("blocked_output", None)
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=issue_number)
                self.set_label(issue_number, LABEL_REWORK)
            return

        self._block(lease, f"unknown lifecycle state: {status}")

    def _parse_canonical_id(self, issue: dict) -> str:
        body = issue.get("body") or ""
        try:
            task = parse_task(body)
            if task.get("issue_id"):
                return str(task["issue_id"])
        except Exception:
            pass
        title = issue.get("title", "")
        match = re.match(r"^\[(?P<tag>issue\d+|[A-Za-z0-9_.-]+)\]", title)
        return match.group("tag") if match else f"issue{issue.get('number', '')}"

    def _evaluate_task_condition(
        self,
        task: dict[str, Any],
        logical_id: str,
        project_issues: list[dict[str, Any]],
    ) -> tuple[bool, str, bool]:
        conditions = task.get("condition") or []
        if not conditions:
            return True, "no dependencies (independent task)", False

        if logical_id in conditions:
            return False, f"self-dependency: {logical_id} depends on itself", True

        issue_by_logical: dict[str, dict[str, Any]] = {}
        for row in project_issues:
            lid = self._parse_canonical_id(row)
            issue_by_logical[lid] = row

        adj: dict[str, list[str]] = {}
        for row in project_issues:
            lid = self._parse_canonical_id(row)
            try:
                t = parse_task(row.get("body") or "")
                adj[lid] = t.get("condition") or []
            except Exception:
                adj[lid] = []
        adj[logical_id] = conditions

        visited: set[str] = set()
        rec_stack: set[str] = set()

        def has_cycle(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for neighbor in adj.get(node, []):
                if neighbor not in visited:
                    if has_cycle(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            rec_stack.remove(node)
            return False

        if has_cycle(logical_id):
            return False, f"dependency cycle involving {logical_id}", True

        for dep_id in conditions:
            dep = issue_by_logical.get(dep_id)
            if not dep:
                return False, f"prerequisite {dep_id} does not exist in repository", False
            dep_labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in dep.get("labels", [])]
            dep_state = str(dep.get("state", "open")).lower()
            is_done = dep_state == "closed" or LABEL_DONE in dep_labels or "orch:done" in dep_labels
            if not is_done:
                return False, f"prerequisite {dep_id} is not DONE (state={dep_state})", False

        return True, "all conditions satisfied", False

    def _reconcile_conditions_for_issues(self, issue_repo: str, issues: list[dict[str, Any]]) -> None:
        for issue in issues:
            labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in issue.get("labels", [])]
            num = int(issue["number"])
            is_waiting = LABEL_WAITING_CONDITION in labels
            is_ready = LABEL_READY in labels
            is_blocked = LABEL_BLOCKED in labels
            if not (is_waiting or is_ready or is_blocked):
                continue
            try:
                task = parse_task(issue.get("body") or "")
                # Only evaluate blocked issues if they have a condition contract
                if is_blocked and not task.get("condition"):
                    continue
                lid = self._parse_canonical_id(issue)
                satisfied, reason, is_invalid = self._evaluate_task_condition(task, lid, issues)
                if is_invalid:
                    if not is_blocked:
                        self.set_label(num, LABEL_BLOCKED, issue_repo)
                        self.event(num, "blocked", post_to_github=False, issue_repo=issue_repo, reason=f"invalid condition: {reason}")
                    continue
                if satisfied and (is_waiting or is_blocked):
                    self.set_label(num, LABEL_READY, issue_repo)
                    if LABEL_WAITING_CONDITION in labels:
                        labels.remove(LABEL_WAITING_CONDITION)
                    if LABEL_BLOCKED in labels:
                        labels.remove(LABEL_BLOCKED)
                    if LABEL_READY not in labels:
                        labels.append(LABEL_READY)
                    issue["labels"] = list(labels)
                elif not satisfied and (is_ready or is_blocked):
                    self.set_label(num, LABEL_WAITING_CONDITION, issue_repo)
                    if LABEL_READY in labels:
                        labels.remove(LABEL_READY)
                    if LABEL_BLOCKED in labels:
                        labels.remove(LABEL_BLOCKED)
                    if LABEL_WAITING_CONDITION not in labels:
                        labels.append(LABEL_WAITING_CONDITION)
                    issue["labels"] = list(labels)
            except Exception:
                pass

    def tick(self) -> None:
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            if not self.state.is_dashboard_verified():
                return
            if not self._github_auth.get("connected"):
                return
            if self.state.is_paused():
                return

            active_leases = self.state.get_active_leases()
            current_active = []
            for l in active_leases:
                if l["instance_id"] != self.instance_id:
                    rec = self.state.takeover_if_stale(self.instance_id, self.settings.lease_timeout, l["issue_number"])
                    if rec:
                        current_active.append(rec)
                else:
                    current_active.append(l)

            for lease in current_active:
                self._handle_active(lease)

            # Available slots for new worker tasks
            busy_count = len([l for l in current_active if l["status"] in ("RUNNING", "REWORK", "VALIDATING", "INTERNAL_REVIEW")])
            available_slots = max(0, self.settings.max_workers - busy_count)
            if available_slots <= 0:
                return

            ready: list[dict[str, Any]] = []
            issue_sources: dict[str, set[str]] = {}
            project_issues_by_repo: dict[str, list[dict[str, Any]]] = {}
            for project in self.registry.list():
                issue_sources.setdefault(project["issues_repo"], set()).add(project["id"])

            for issue_repo, project_ids in issue_sources.items():
                ready_issues = self.github.list_ready_issues(issue_repo)
                ready_mocked = not (hasattr(self.github.list_ready_issues, "__self__") and self.github.list_ready_issues.__self__ is self.github)
                open_mocked = hasattr(self.github, "list_open_orchestrator_issues") and not (hasattr(self.github.list_open_orchestrator_issues, "__self__") and self.github.list_open_orchestrator_issues.__self__ is self.github)
                if ready_mocked and not open_mocked:
                    open_issues = list(ready_issues)
                else:
                    try:
                        open_issues = self.github.list_open_orchestrator_issues(issue_repo)
                    except Exception:
                        open_issues = list(ready_issues)
                known_numbers = {r["number"] for r in open_issues}
                for r in ready_issues:
                    if r["number"] not in known_numbers:
                        open_issues.append(r)
                if open_mocked or ready_mocked:
                    all_repo_issues = list(open_issues)
                else:
                    try:
                        all_repo_issues = self.github.list_all_orchestrator_issues(issue_repo)
                    except Exception:
                        all_repo_issues = list(open_issues)
                project_issues_by_repo[issue_repo] = all_repo_issues
                self._reconcile_conditions_for_issues(issue_repo, all_repo_issues)

                for row in all_repo_issues:
                    if str(row.get("state", "open")).lower() != "open":
                        continue
                    labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in row.get("labels", [])]
                    if LABEL_READY in labels or LABEL_REWORK in labels:
                        issue = dict(row)
                        issue["_issue_repo"] = issue_repo
                        issue["_allowed_projects"] = sorted(project_ids)
                        issue["_is_rework"] = LABEL_REWORK in labels
                        ready.append(issue)

            def priority(issue: dict) -> tuple[int, int, str]:
                try:
                    task = parse_task(issue.get("body") or "")
                    return int(task.get("priority", 9)), int(issue["number"]), issue["_issue_repo"]
                except Exception:
                    return 99, int(issue["number"]), issue["_issue_repo"]

            for issue in sorted(ready, key=priority):
                if available_slots <= 0:
                    break
                num = int(issue["number"])
                issue_repo = issue["_issue_repo"]
                if any(l["issue_number"] == num for l in current_active):
                    continue

                logical_id = self._parse_canonical_id(issue)
                try:
                    author = ((issue.get("user") or {}).get("login") or "")
                    if author not in self.settings.allowed_authors:
                        self.set_label(num, LABEL_BLOCKED, issue_repo)
                        self.event(
                            num,
                            "blocked",
                            post_to_github=False,
                            issue_repo=issue_repo,
                            logical_issue_id=logical_id,
                            reason=f"unauthorized issue author: {author}",
                        )
                        continue

                    task = parse_task(issue.get("body") or "")
                    contract_issue_id = task.get("issue_id")
                    if contract_issue_id and contract_issue_id != logical_id:
                        self.set_label(num, LABEL_BLOCKED, issue_repo)
                        self.event(
                            num,
                            "blocked",
                            post_to_github=False,
                            issue_repo=issue_repo,
                            logical_issue_id=logical_id,
                            reason=f"task contract issue_id '{contract_issue_id}' does not match title tag '{logical_id}'",
                        )
                        continue

                    all_repo_issues = project_issues_by_repo.get(issue_repo, [])
                    satisfied, reason, is_invalid = self._evaluate_task_condition(task, logical_id, all_repo_issues)
                    if is_invalid:
                        self.set_label(num, LABEL_BLOCKED, issue_repo)
                        self.event(
                            num,
                            "blocked",
                            post_to_github=False,
                            issue_repo=issue_repo,
                            logical_issue_id=logical_id,
                            reason=f"invalid condition: {reason}",
                        )
                        continue
                    if not satisfied:
                        self.set_label(num, LABEL_WAITING_CONDITION, issue_repo)
                        continue

                    if task["project"] not in issue["_allowed_projects"]:
                        self.set_label(num, LABEL_BLOCKED, issue_repo)
                        self.event(
                            num,
                            "blocked",
                            post_to_github=False,
                            issue_repo=issue_repo,
                            logical_issue_id=logical_id,
                            reason=(
                                f"task project {task['project']} is not registered to Issue repo {issue_repo}; "
                                f"allowed={issue['_allowed_projects']}"
                            ),
                        )
                        continue

                    payload = {
                        "issue_repo": issue_repo,
                        "task_id": task["task_id"],
                        "logical_issue_id": logical_id,
                        "revision": int(task["revision"]),
                        "contract_hash": canonical_task_hash(task),
                        "approved_gates": {},
                        "rework_count": 0,
                        "review_cycle": 0,
                    }
                    initial_status = "REWORK" if issue.get("_is_rework") else "RUNNING"
                    if not self.state.claim(self.instance_id, num, initial_status, payload, max_workers=self.settings.max_workers):
                        continue
                    lease = self.state.get_lease(num)
                    assert lease
                    current_active.append(lease)
                    available_slots -= 1
                    if initial_status == "RUNNING":
                        self.set_label(num, LABEL_RUNNING, issue_repo)
                    self.event(num, "started", post_to_github=False, task_id=task["task_id"], project=task["project"])
                    self._handle_active(lease)
                except Exception as exc:
                    lease = self.state.get_lease(num)
                    if lease and lease["instance_id"] == self.instance_id:
                        self._block(lease, f"runtime failure: {exc}")
                    else:
                        self.set_label(num, LABEL_BLOCKED, issue_repo)
                        self.event(
                            num,
                            "blocked",
                            post_to_github=False,
                            issue_repo=issue_repo,
                            logical_issue_id=logical_id,
                            reason=f"task claim failure: {exc}",
                        )
        finally:
            self._tick_lock.release()

    def _git_local_task_issues(self) -> list[dict[str, Any]]:
        """Extract task issues from local and remote Git branches and SQLite state leases
        when GitHub API is offline or unauthenticated."""
        issues: list[dict[str, Any]] = []
        seen_numbers: set[int] = set()

        # 1. Add any leases in SQLite state
        for lease in self.state.get_active_leases():
            num = int(lease["issue_number"])
            seen_numbers.add(num)
            p = dict(lease.get("payload") or {})
            lc = self._lifecycle(lease["status"], final_reviewer=self.get_final_reviewer())
            issues.append({
                "issue_repo": p.get("issue_repo") or p.get("target_repo", ""),
                "project_ids": [p.get("project", "")],
                "number": num,
                "title": f"Task #{num}: {p.get('task_id', 'Active Task')}",
                "url": f"https://github.com/{p.get('target_repo', '')}/issues/{num}",
                "labels": ["orch:running" if lease["status"] == "RUNNING" else "orch:rework"],
                "task": {
                    "task_id": p.get("task_id", f"task-{num}"),
                    "project": p.get("project", ""),
                    "type": "code",
                    "revision": int(p.get("revision", 1)),
                    "priority": 1,
                    "issue_id": p.get("logical_issue_id"),
                    "condition": [],
                },
                "task_error": None,
                "lifecycle": lc,
            })

        # 2. Extract from Git branches in managed project repos
        branch_pat = re.compile(r"task/issue-(?P<number>\d+)-(?P<slug>.+)$")
        for project in self.registry.list():
            root = self.workspace.repo_dir(project["repo"])
            if not root.exists():
                continue
            proc = subprocess.run(
                ["git", "branch", "-a"],
                cwd=root,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if proc.returncode != 0:
                continue
            for line in proc.stdout.splitlines():
                raw = line.strip().replace("*", "").strip()
                if " -> " in raw:
                    continue
                clean_branch = raw.replace("remotes/origin/", "").replace("origin/", "")
                m = branch_pat.match(clean_branch)
                if not m:
                    continue
                num = int(m.group("number"))
                if num in seen_numbers:
                    continue
                seen_numbers.add(num)
                slug = m.group("slug")

                subj_proc = subprocess.run(
                    ["git", "log", "-1", "--format=%s", raw],
                    cwd=root,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                subject = subj_proc.stdout.strip() or f"Task issue #{num} ({slug})"
                lc = self._lifecycle("RUNNING", ["orch:running"], final_reviewer=self.get_final_reviewer())

                issues.append({
                    "issue_repo": project.get("issues_repo", project["repo"]),
                    "project_ids": [project["id"]],
                    "number": num,
                    "title": subject,
                    "url": f"https://github.com/{project['repo']}/issues/{num}",
                    "labels": ["orch:running"],
                    "task": {
                        "task_id": slug,
                        "project": project["id"],
                        "type": "code",
                        "revision": 1,
                        "priority": 2,
                        "issue_id": slug.upper(),
                        "condition": [],
                    },
                    "task_error": None,
                    "lifecycle": lc,
                })

        return sorted(issues, key=lambda x: x["number"])

    # ---------- dashboard operations ----------
    def snapshot(self) -> dict[str, Any]:
        dashboard_bootstrap = self.state.dashboard_verification()
        github_auth = self.github_auth_status()
        if not dashboard_bootstrap["verified"]:
            queue = []
            queue_error = "dashboard bootstrap not verified; managed-project Issue queue is disabled"
        elif not github_auth.get("connected"):
            queue = self._git_local_task_issues()
            queue_error = "GitHub API chưa kết nối (thiếu GITHUB_TOKEN). Đang tải task từ Git branches và local state."
        else:
            try:
                queue = []
                sources: dict[str, list[str]] = {}
                for project in self.registry.list():
                    sources.setdefault(project["issues_repo"], []).append(project["id"])
                open_mocked = hasattr(self.github, "list_open_orchestrator_issues") and not (
                    hasattr(self.github.list_open_orchestrator_issues, "__self__")
                    and self.github.list_open_orchestrator_issues.__self__ is self.github
                )
                for issue_repo, project_ids in sorted(sources.items()):
                    if open_mocked:
                        issues_list = self.github.list_open_orchestrator_issues(issue_repo)
                    else:
                        try:
                            issues_list = self.github.list_all_orchestrator_issues(issue_repo)
                        except Exception:
                            issues_list = self.github.list_open_orchestrator_issues(issue_repo)
                    for row in issues_list:
                        task_summary = None
                        task_error = None
                        try:
                            parsed = parse_task(row.get("body") or "")
                            task_summary = {
                                "task_id": parsed["task_id"],
                                "project": parsed["project"],
                                "type": parsed["type"],
                                "revision": int(parsed["revision"]),
                                "priority": int(parsed.get("priority", 9)),
                                "issue_id": parsed.get("issue_id"),
                                "condition": parsed.get("condition", []),
                            }
                        except Exception as exc:
                            task_error = str(exc)
                        labels = [label["name"] for label in row.get("labels", []) if isinstance(label, dict) and "name" in label] or [str(l) for l in row.get("labels", [])]
                        active_lease = self.state.get_lease(int(row["number"]))
                        active_status = ""
                        active_payload = {}
                        if (
                            active_lease
                            and int(active_lease["issue_number"]) == int(row["number"])
                            and str(active_lease["payload"].get("issue_repo")) == issue_repo
                        ):
                            active_status = active_lease["status"]
                            active_payload = dict(active_lease.get("payload") or {})
                        elif str(row.get("state", "open")).lower() == "closed":
                            active_status = "DONE"
                        lifecycle = self._lifecycle(active_status, labels, final_reviewer=self.get_final_reviewer())
                        if active_status == "BLOCKED" or lifecycle.get("is_blocked"):
                            lifecycle["blocked_reason"] = active_payload.get("blocked_reason") or ""
                            lifecycle["blocked_output"] = active_payload.get("blocked_output") or self.get_task_log(int(row["number"]))
                        queue.append({
                            "issue_repo": issue_repo,
                            "project_ids": sorted(project_ids),
                            "number": row["number"],
                            "title": row["title"],
                            "state": str(row.get("state", "open")).lower(),
                            "url": row.get("html_url"),
                            "labels": labels,
                            "task": task_summary,
                            "task_error": task_error,
                            "lifecycle": lifecycle,
                        })
                queue_error = None
            except Exception as exc:
                queue, queue_error = self._git_local_task_issues(), str(exc)


        active_leases = self.state.get_active_leases()
        active_tasks = []
        for l in active_leases:
            p = dict(l.get("payload") or {})
            lc = self._lifecycle(l["status"], final_reviewer=self.get_final_reviewer())
            task_log = self.get_task_log(l["issue_number"])
            if l["status"] == "BLOCKED" or lc.get("is_blocked"):
                lc["blocked_reason"] = p.get("blocked_reason") or ""
                lc["blocked_output"] = p.get("blocked_output") or task_log
            active_tasks.append({
                "issue_number": l["issue_number"],
                "status": l["status"],
                "instance_id": l["instance_id"],
                "payload": p,
                "lifecycle": lc,
                "task_log": task_log,
                "heartbeat": l.get("heartbeat"),
                "created_at": l.get("created_at"),
            })
        active = active_tasks[0] if active_tasks else None
        active_lifecycle = active["lifecycle"] if active else self._lifecycle("READY", final_reviewer=self.get_final_reviewer())

        metrics = {
            "ready": sum(1 for x in queue if x.get("lifecycle", {}).get("status") == "READY"),
            "in_progress": sum(1 for x in queue if x.get("lifecycle", {}).get("stage_index") in (1, 2, 3)),
            "review": sum(1 for x in queue if x.get("lifecycle", {}).get("stage_index") == 4),
            "blocked": sum(1 for x in queue if x.get("lifecycle", {}).get("is_blocked")),
            "waiting_condition": sum(1 for x in queue if x.get("lifecycle", {}).get("status") == "WAITING_CONDITION"),
            "done": sum(1 for x in queue if x.get("lifecycle", {}).get("stage_index") == 5),
            "total_open": len([x for x in queue if str(x.get("state", "open")).lower() == "open"]),
            "total_tasks": len(queue),
        }
        return {
            "instance_id": self.instance_id,
            "paused": self.state.is_paused(),
            "dashboard_bootstrap": dashboard_bootstrap,
            "github_auth": github_auth,
            "agy_config": {
                "model": self.get_agy_model(),
                "available_models": self.get_available_agy_models(),
            },
            "final_reviewer_config": {
                "final_reviewer": self.get_final_reviewer(),
                "gemini_reviewer_model": self.get_gemini_reviewer_model(),
                "available_gemini_models": self.get_available_gemini_models(),
            },
            "telegram": self.telegram.get_config(masked=False),
            "self_update": self.self_update_status(),
            "system": {
                "managed_root": str(self.settings.workspace_root),
                "git_transport": self.settings.git_transport,
                "github_api_auth": "gh-cli",
            },
            "auto_sync": {
                "last_sync_at": self._last_sync_at,
                "last_sync_datetime": getattr(self, "_last_sync_datetime", None),
                "last_results": self._last_sync_results,
            },
            "worker_capacity": {
                "active": len([t for t in active_tasks if t["status"] in ("RUNNING", "REWORK", "VALIDATING", "INTERNAL_REVIEW")]),
                "total_leases": len(active_tasks),
                "max": self.settings.max_workers,
            },
            "active": active,
            "active_lifecycle": active_lifecycle,
            "active_tasks": active_tasks,
            "metrics": metrics,
            "projects": self.project_snapshot(queue),
            "issues": queue,
            "issues_error": queue_error,
            "events": self.state.recent_events(100),
        }

    def pause(self) -> None:
        self.state.set_paused(True)
        self.state.add_event("paused", {})

    def resume(self) -> None:
        self.state.set_paused(False)
        self.state.add_event("resumed", {})

    def request_retry(self, issue_number: int | None = None) -> None:
        if not self.state.is_dashboard_verified():
            raise RuntimeError("Dashboard bootstrap has not been verified; Issue operations are disabled")
        lease = self.state.get_lease(issue_number=issue_number)
        if not lease:
            raise RuntimeError(f"No active task found{' for issue #' + str(issue_number) if issue_number else ''}")
        target_num = lease["issue_number"]
        payload = dict(lease["payload"])
        payload.pop("blocked_reason", None)
        payload.pop("blocked_output", None)
        payload.pop("transient_retry_count", None)
        self.state.update_lease(self.instance_id, status="REWORK", payload=payload, issue_number=target_num)
        self.state.add_event("retry_requested_from_dashboard", {"issue_number": target_num})
        try:
            repo = self._issue_repo(target_num, payload.get("issue_repo"))
            self.telegram.send_event_async("retry_requested", {
                "issue_number": target_num,
                "issue_repo": repo,
                "task_id": payload.get("task_id"),
                "title": payload.get("title"),
            })
        except Exception:
            pass
        if self._github_auth.get("connected"):
            try:
                self.set_label(target_num, LABEL_REWORK)
            except Exception:
                pass

    def update_graphify(self, project_id: str) -> dict[str, Any]:
        project = self.registry.resolve(project_id)
        root = self.workspace.sync_project(
            project["repo"],
            project.get("default_branch"),
        )
        catalog = load_catalog(root, project.get("manifest_path", ".orchestrator/project.json"))
        status = self.graphify.ensure_graph(root, catalog["manifest"])
        self.state.add_event("graphify_updated", {"project": project_id, "status": status})
        return status

    def serve_loop(self, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.state.add_event("tick_error", {"error": str(exc)})
            stop_event.wait(self.settings.poll_interval)

    def serve_sync_loop(self, stop_event: threading.Event | None = None, interval: int = 15) -> None:
        stop_event = stop_event or threading.Event()
        interval = max(5, int(interval))
        while not stop_event.is_set():
            try:
                self.sync_projects()
            except Exception as exc:
                self.state.add_event("sync_error", {"error": str(exc)})
            stop_event.wait(interval)
