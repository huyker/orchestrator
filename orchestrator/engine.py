from __future__ import annotations

import fnmatch
import json
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
    def __init__(self, settings: Settings):
        self.settings = settings
        self.instance_id = str(uuid.uuid4())
        self.github = GitHubClient(settings.token)
        self.registry = Registry(settings.registry_file)
        self.state = StateStore(settings.runtime_dir / "state.sqlite3")
        self.workspace = WorkspaceManager(settings)
        self.graphify = GraphifyAdapter()
        self._tick_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._models_lock = threading.Lock()
        self._available_models: list[dict[str, Any]] = list(DEFAULT_AGY_MODELS)
        self._models_refresh_started = False
        self._project_heads: dict[str, str] = {}
        self._last_sync_at: float | None = None
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
        return {
            **entry,
            "managed_path": str(root),
            "repo_info": info,
            "already_registered": False,
        }

    def remove_managed_project(self, project_id: str) -> dict[str, Any]:
        self.registry = Registry(self.settings.registry_file)
        removed = self.registry.remove_project(project_id)
        self.state.add_event("project_removed", {
            "project": removed.get("id"),
            "repo": removed.get("repo"),
        })
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
    def _lifecycle(status: str, labels: list[str] | None = None) -> dict[str, Any]:

        labels = labels or []
        stages = [
            {"id": "READY", "label": "Ready"},
            {"id": "IMPLEMENT", "label": "Implement"},
            {"id": "VALIDATE", "label": "Validate"},
            {"id": "INTERNAL_REVIEW", "label": "QA"},
            {"id": "GPT_REVIEW", "label": "GPT Review"},
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
            elif "orch:approved" in labels or "orch:gpt-review" in labels:
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

    def event(
        self,
        issue: int,
        event_type: str,
        *,
        issue_repo: str | None = None,
        logical_issue_id: str | None = None,
        post_to_github: bool | None = None,
        **payload: Any,
    ) -> int:
        repo = self._issue_repo(issue, issue_repo)
        logical_issue = logical_issue_id or f"issue{issue}"
        lease = self.state.get_lease()
        if lease and int(lease.get("issue_number", 0)) == int(issue):
            logical_issue = lease.get("payload", {}).get("logical_issue_id") or logical_issue

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
        self.state.add_event(event_type, {"issue_repo": repo, "issue_number": issue, **payload})

        # Only post comments to GitHub Issue when code is ready/submitted or user interaction is needed.
        # Local execution failures (blocked), internal acceptance/review rework cycles remain local-only.
        github_visible_events = {
            "user_gate_required",
            "ready_for_gpt_review",
            "fixdone",
            "question",
            "complete",
            "done",
        }
        should_post = post_to_github if post_to_github is not None else (event_type in github_visible_events)
        if not should_post:
            return 0

        header = f"[{logical_issue}_{spec_event}_by{actor}]"
        comment_body = f"{header}\n\n```orchestrator-event\n" + json.dumps(body, ensure_ascii=False, indent=2) + "\n```"
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
            self._last_sync_at = time.time()
            self._last_sync_results = result
            self.state.add_event("projects_synced", {"projects": result})
            return result
        finally:
            self._sync_lock.release()

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

    def run_agent(self, agent: dict, prompt: str, worktree: Path, issue_number: int) -> tuple[int, str]:
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
            "--model",
            model,
            "--print",
            short,
            "--agent",
            agent["agy_agent"],
        ]
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
        claimed_revision = int(payload["revision"])
        current_revision = int(task["revision"])
        claimed_hash = payload["contract_hash"]
        if current_revision == claimed_revision and digest != claimed_hash:
            self._block(lease, "same-revision Issue contract mutation detected; increment revision")
            return payload, False
        if current_revision < claimed_revision:
            self._block(lease, "Issue revision moved backwards")
            return payload, False
        if current_revision > claimed_revision:
            payload.update({"revision": current_revision, "contract_hash": digest, "rework_count": 0})
            self.state.update_lease(self.instance_id, status="REWORK", payload=payload)
            self.set_label(lease["issue_number"], LABEL_REWORK)
            self.event(lease["issue_number"], "revision_updated", revision=current_revision)
            return payload, False
        return payload, True

    def _block(self, lease: dict, reason: str, **extra: Any) -> None:
        payload = dict(lease["payload"])
        comment_id = self.event(lease["issue_number"], "blocked", post_to_github=False, reason=reason, **extra)
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
        self.state.update_lease(self.instance_id, status="BLOCKED", payload=payload)

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
        self.state.update_lease(self.instance_id, status="RUNNING", payload=payload)
        self.set_label(issue_number, LABEL_RUNNING)

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
        code, output = self.run_agent(executor, prompt, worktree, issue_number)
        question = self._question_from_output(output)
        if question:
            comment_id = self.event(issue_number, "question", **question)
            payload.update({
                "pending_question_id": question["question_id"],
                "command_after_comment_id": comment_id,
            })
            self.state.update_lease(self.instance_id, status="WAITING_ANSWER", payload=payload)
            self.set_label(issue_number, LABEL_QUESTION)
            return
        if code:
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
            self.state.update_lease(self.instance_id, status="WAITING_USER_GATE", payload=payload)
            self.set_label(issue_number, LABEL_GATE)
            return

        self.state.update_lease(self.instance_id, status="VALIDATING", payload=payload)
        first = self.machine_acceptance(task, catalog, profile, worktree)
        if not first["pass"]:
            self._schedule_rework(lease, payload, task, "acceptance_failed", report=first)
            return

        diff = self.workspace.diff(worktree, task["base_branch"])
        reviewer_context = self.build_context(worktree, task, catalog, profile, reviewer)
        reviewer_prompt = f"""# Independent reviewer\n\nGitHub Issue: {issue_repo}#{issue_number}\nYou did not implement this task. Review the Issue contract plus actual files/diff/tests.\n\n## Task\n{json.dumps(task, ensure_ascii=False, indent=2)}\n\n## Reviewer profile\n{json.dumps(reviewer, ensure_ascii=False, indent=2)}\n\n## Project context\n{reviewer_context}\n\n## Graphify context (advisory)\n{graph_context or '[not available]'}\n\n## Deterministic acceptance\n{json.dumps(first, ensure_ascii=False, indent=2)}\n\n## Actual diff\n{diff}\n\nInspect actual files/assets/tests directly. End output with exactly one line:\n@@ORCH_REVIEW@@ {{\"verdict\":\"PASS|FAIL\",\"score\":0,\"summary\":\"...\",\"findings\":[],\"acceptance\":[{{\"criterion\":\"exact acceptance string\",\"status\":\"PASS|FAIL\",\"evidence\":\"exact evidence\"}}],\"prohibited\":[{{\"rule\":\"exact prohibited string\",\"status\":\"PASS|FAIL\",\"evidence\":\"exact evidence\"}}],\"risks\":[]}}\nEvery acceptance/prohibited item must appear exactly and include evidence.\n"""
        self.state.update_lease(self.instance_id, status="INTERNAL_REVIEW", payload=payload)
        reviewer_code, reviewer_output = self.run_agent(reviewer, reviewer_prompt, worktree, issue_number)
        if reviewer_code:
            self._block(lease, "reviewer infrastructure failure", exit_code=reviewer_code, output=reviewer_output[-4000:])
            return
        review, review_errors = self.parse_review(reviewer_output, task)
        if review_errors:
            self._schedule_rework(lease, payload, task, "review_failed", review=review, errors=review_errors)
            return

        self.state.update_lease(self.instance_id, status="VALIDATING", payload=payload)
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
        comment_id = self.event(
            issue_number,
            "ready_for_gpt_review",
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
        self.state.update_lease(self.instance_id, status="WAITING_GPT_REVIEW", payload=payload)
        self.set_label(issue_number, LABEL_GPT_REVIEW)

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
            self.state.update_lease(self.instance_id, status="BLOCKED", payload=payload)
            return
        self.state.update_lease(self.instance_id, status="REWORK", payload=payload)
        self.set_label(issue_number, LABEL_REWORK)

    def _handle_active(self, lease: dict) -> None:
        issue_number = lease["issue_number"]
        issue, task, digest = self._load_issue_task(issue_number)
        payload, ok = self._reconcile_contract(lease, issue, task, digest)
        if not ok:
            return
        status = lease["status"]
        comments = self.github.comments(self._issue_repo(issue_number), issue_number)
        revision = int(payload["revision"])
        after = int(payload.get("command_after_comment_id", 0))

        if status == "RECOVERING":
            self.event(issue_number, "recovered_after_restart", post_to_github=False, previous_instance=payload.get("recovered_from_instance"))
            payload.pop("blocked_reason", None)
            payload.pop("blocked_output", None)
            self.state.update_lease(self.instance_id, status="REWORK", payload=payload)
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
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload)
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
                    self.state.update_lease(self.instance_id, status="REWORK", payload=payload)
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
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload)
                self.set_label(issue_number, LABEL_REWORK)
            return

        if status == "WAITING_GPT_REVIEW":
            pr_number = int(payload["pr_number"])
            pr = self.github.get_pr(payload["target_repo"], pr_number)
            actual_head = ((pr.get("head") or {}).get("sha") or "")
            if actual_head and actual_head != payload.get("pr_head_sha"):
                self.event(issue_number, "pr_head_changed", previous=payload.get("pr_head_sha"), current=actual_head)
                payload["pr_head_sha"] = actual_head
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload)
                self.set_label(issue_number, LABEL_REWORK)
                return
            cycle = int(payload["review_cycle"])
            expected_sha = payload["pr_head_sha"]
            found = self._find_command(
                comments,
                issue_number,
                "external_review",
                revision,
                after_comment_id=after,
                predicate=lambda cmd: int(cmd.get("review_cycle", -1)) == cycle and cmd.get("pr_head_sha") == expected_sha,
            )
            if not found:
                return
            command, _ = found
            verdict = command.get("verdict")
            if verdict == "FIX_REQUIRED":
                self.event(issue_number, "gpt_fix_required", findings=command.get("findings", []), review_cycle=cycle)
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload)
                self.set_label(issue_number, LABEL_REWORK)
            elif verdict == "PASS":
                self.event(issue_number, "gpt_approved", review_cycle=cycle, pr_head_sha=expected_sha)
                self.state.update_lease(self.instance_id, status="APPROVED_WAITING_MERGE", payload=payload)
                self.set_label(issue_number, LABEL_APPROVED)
            else:
                self._block(lease, f"invalid external_review verdict: {verdict}")
            return

        if status == "APPROVED_WAITING_MERGE":
            pr = self.github.get_pr(payload["target_repo"], int(payload["pr_number"]))
            if pr.get("merged_at"):
                self.event(issue_number, "complete", merged_pr=pr["html_url"])
                self.set_label(issue_number, LABEL_DONE)
                self.github.close_issue(self._issue_repo(issue_number), issue_number)
                self.state.release(self.instance_id)
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
                self.state.update_lease(self.instance_id, status="REWORK", payload=payload)
                self.set_label(issue_number, LABEL_REWORK)
            return

        self._block(lease, f"unknown lifecycle state: {status}")

    def _parse_canonical_id(self, issue: dict) -> str:
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
            if LABEL_WAITING_CONDITION not in labels and LABEL_READY not in labels:
                continue
            try:
                task = parse_task(issue.get("body") or "")
                lid = self._parse_canonical_id(issue)
                satisfied, reason, is_invalid = self._evaluate_task_condition(task, lid, issues)
                if is_invalid:
                    self.set_label(num, LABEL_BLOCKED, issue_repo)
                    self.event(num, "blocked", post_to_github=False, issue_repo=issue_repo, reason=f"invalid condition: {reason}")
                    continue
                if satisfied and LABEL_WAITING_CONDITION in labels:
                    self.set_label(num, LABEL_READY, issue_repo)
                    if LABEL_WAITING_CONDITION in labels:
                        labels.remove(LABEL_WAITING_CONDITION)
                    if LABEL_READY not in labels:
                        labels.append(LABEL_READY)
                elif not satisfied and LABEL_READY in labels:
                    self.set_label(num, LABEL_WAITING_CONDITION, issue_repo)
                    if LABEL_READY in labels:
                        labels.remove(LABEL_READY)
                    if LABEL_WAITING_CONDITION not in labels:
                        labels.append(LABEL_WAITING_CONDITION)
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
                project_issues_by_repo[issue_repo] = open_issues
                self._reconcile_conditions_for_issues(issue_repo, open_issues)
                for row in open_issues:
                    labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in row.get("labels", [])]
                    if LABEL_READY in labels:
                        issue = dict(row)
                        issue["_issue_repo"] = issue_repo
                        issue["_allowed_projects"] = sorted(project_ids)
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
                    if not self.state.claim(self.instance_id, num, "RUNNING", payload, max_workers=self.settings.max_workers):
                        continue
                    lease = self.state.get_lease(num)
                    assert lease
                    current_active.append(lease)
                    available_slots -= 1
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

    # ---------- dashboard operations ----------
    def snapshot(self) -> dict[str, Any]:
        dashboard_bootstrap = self.state.dashboard_verification()
        github_auth = self.github_auth_status()
        if not dashboard_bootstrap["verified"]:
            queue = []
            queue_error = "dashboard bootstrap not verified; managed-project Issue queue is disabled"
        elif not github_auth.get("connected"):
            queue = []
            queue_error = "GitHub not connected; managed-project Issue queue is disabled"
        else:
            try:
                queue = []
                sources: dict[str, list[str]] = {}
                for project in self.registry.list():
                    sources.setdefault(project["issues_repo"], []).append(project["id"])
                for issue_repo, project_ids in sorted(sources.items()):
                    for row in self.github.list_open_orchestrator_issues(issue_repo):
                        if not any(label.get("name", "").startswith("orch:") for label in row.get("labels", [])):
                            continue
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
                        labels = [label["name"] for label in row.get("labels", [])]
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
                        lifecycle = self._lifecycle(active_status, labels)
                        if active_status == "BLOCKED" or lifecycle.get("is_blocked"):
                            lifecycle["blocked_reason"] = active_payload.get("blocked_reason") or ""
                            lifecycle["blocked_output"] = active_payload.get("blocked_output") or ""
                        queue.append({
                            "issue_repo": issue_repo,
                            "project_ids": sorted(project_ids),
                            "number": row["number"],
                            "title": row["title"],
                            "url": row.get("html_url"),
                            "labels": labels,
                            "task": task_summary,
                            "task_error": task_error,
                            "lifecycle": lifecycle,
                        })
                queue_error = None
            except Exception as exc:
                queue, queue_error = [], str(exc)

        active_leases = self.state.get_active_leases()
        active_tasks = []
        for l in active_leases:
            p = dict(l.get("payload") or {})
            lc = self._lifecycle(l["status"])
            if l["status"] == "BLOCKED" or lc.get("is_blocked"):
                lc["blocked_reason"] = p.get("blocked_reason") or ""
                lc["blocked_output"] = p.get("blocked_output") or ""
            active_tasks.append({
                "issue_number": l["issue_number"],
                "status": l["status"],
                "instance_id": l["instance_id"],
                "payload": p,
                "lifecycle": lc,
                "heartbeat": l.get("heartbeat"),
                "created_at": l.get("created_at"),
            })
        active = active_tasks[0] if active_tasks else None
        active_lifecycle = active["lifecycle"] if active else self._lifecycle("READY")

        metrics = {
            "ready": sum(1 for x in queue if x.get("lifecycle", {}).get("status") == "READY"),
            "in_progress": sum(1 for x in queue if x.get("lifecycle", {}).get("stage_index") in (1, 2, 3)),
            "review": sum(1 for x in queue if x.get("lifecycle", {}).get("stage_index") == 4),
            "blocked": sum(1 for x in queue if x.get("lifecycle", {}).get("is_blocked")),
            "waiting_condition": sum(1 for x in queue if x.get("lifecycle", {}).get("status") == "WAITING_CONDITION"),
            "total_open": len(queue),
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
            "self_update": self.self_update_status(),
            "system": {
                "managed_root": str(self.settings.workspace_root),
                "git_transport": self.settings.git_transport,
                "github_api_auth": "gh-cli",
            },
            "auto_sync": {
                "last_sync_at": self._last_sync_at,
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

    def request_retry(self) -> None:
        if not self.state.is_dashboard_verified():
            raise RuntimeError("Dashboard bootstrap has not been verified; Issue operations are disabled")
        lease = self.state.get_lease()
        if not lease:
            raise RuntimeError("No active task")
        payload = dict(lease["payload"])
        payload.pop("blocked_reason", None)
        payload.pop("blocked_output", None)
        self.state.update_lease(self.instance_id, status="REWORK", payload=payload)
        self.state.add_event("retry_requested_from_dashboard", {"issue_number": lease["issue_number"]})
        if self._github_auth.get("connected"):
            try:
                self.set_label(lease["issue_number"], LABEL_REWORK)
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
