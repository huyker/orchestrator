from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASK_BLOCK = re.compile(r"```orchestrator-task\s*(\{.*?\})\s*```", re.S)
COMMAND_BLOCK = re.compile(r"```orchestrator-command\s*(\{.*?\})\s*```", re.S)
EVENT_MARKER = "@@ORCH_EVENT@@ "
REVIEW_MARKER = "@@ORCH_REVIEW@@ "

LABEL_READY = "orch:ready"
LABEL_WAITING_CONDITION = "orch:waiting-condition"
LABEL_RUNNING = "orch:running"
LABEL_QUESTION = "orch:question"
LABEL_GATE = "orch:user-gate"
LABEL_REWORK = "orch:rework"
LABEL_GPT_REVIEW = "orch:gpt-review"
LABEL_APPROVED = "orch:approved"
LABEL_DONE = "orch:done"
LABEL_BLOCKED = "orch:blocked"

ALL_LABELS = {
    LABEL_READY,
    LABEL_WAITING_CONDITION,
    LABEL_RUNNING,
    LABEL_QUESTION,
    LABEL_GATE,
    LABEL_REWORK,
    LABEL_GPT_REVIEW,
    LABEL_APPROVED,
    LABEL_DONE,
    LABEL_BLOCKED,
}


@dataclass(frozen=True)
class Settings:
    control_repo: str
    token: str
    registry_file: Path
    runtime_dir: Path
    workspace_root: Path
    poll_interval: int
    git_transport: str
    agy_bin: str
    agent_effort: str
    agent_timeout: int
    test_timeout: int
    lease_timeout: int
    dashboard_host: str
    dashboard_port: int
    allowed_authors: tuple[str, ...]
    agy_model: str = "gemini-3.8-flash-high"
    max_workers: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        control_repo = os.getenv("ORCH_CONTROL_REPO", "").strip()
        token = ""
        if control_repo and "/" not in control_repo:
            raise ValueError("ORCH_CONTROL_REPO must be OWNER/REPO when provided")

        registry_file = Path(os.getenv("ORCH_PROJECT_REGISTRY", "projects.json")).resolve()
        runtime = Path(os.getenv("ORCH_RUNTIME_DIR", ".orchestrator-runtime")).resolve()

        raw_authors = os.getenv("ORCH_ALLOWED_AUTHORS", "").strip()
        if not raw_authors:
            owners: set[str] = set()
            try:
                registry = json.loads(registry_file.read_text(encoding="utf-8"))
                for project in registry.get("projects", []):
                    repo = str(project.get("issues_repo") or project.get("repo") or "")
                    if "/" in repo:
                        owners.add(repo.split("/", 1)[0].strip())
            except (OSError, json.JSONDecodeError, TypeError):
                pass
            if control_repo and "/" in control_repo:
                owners.add(control_repo.split("/", 1)[0].strip())
            raw_authors = ",".join(sorted(x for x in owners if x))
        if not raw_authors:
            raise ValueError(
                "ORCH_ALLOWED_AUTHORS could not be inferred from projects.json; "
                "set ORCH_ALLOWED_AUTHORS explicitly"
            )

        agy_model = os.getenv("ORCH_AGY_MODEL", "gemini-3.8-flash-high").strip() or "gemini-3.8-flash-high"
        max_workers = max(1, int(os.getenv("ORCH_MAX_WORKERS", "2")))

        return cls(
            control_repo=control_repo,
            token=token,
            registry_file=registry_file,
            runtime_dir=runtime,
            workspace_root=Path(os.getenv("ORCH_MANAGED_ROOT", runtime / "managed-projects")).resolve(),
            poll_interval=max(2, int(os.getenv("ORCH_POLL_INTERVAL", "5"))),
            git_transport=os.getenv("ORCH_GIT_TRANSPORT", "ssh").strip().lower(),
            agy_bin=os.getenv("ORCH_AGY_BIN", "agy").strip(),
            agent_effort=os.getenv("ORCH_AGENT_EFFORT", "medium").strip(),
            agent_timeout=max(30, int(os.getenv("ORCH_AGENT_TIMEOUT", "1800"))),
            test_timeout=max(5, int(os.getenv("ORCH_TEST_TIMEOUT", "600"))),
            lease_timeout=max(30, int(os.getenv("ORCH_LEASE_TIMEOUT", "90"))),
            dashboard_host=os.getenv("ORCH_DASHBOARD_HOST", "127.0.0.1").strip(),
            dashboard_port=int(os.getenv("ORCH_DASHBOARD_PORT", "8766")),
            allowed_authors=tuple(x.strip() for x in raw_authors.split(",") if x.strip()),
            agy_model=agy_model,
            max_workers=max_workers,
        )


def parse_task(body: str) -> dict[str, Any]:
    matches = list(TASK_BLOCK.finditer(body or ""))
    if len(matches) != 1:
        raise ValueError("Issue body must contain exactly one orchestrator-task JSON block")
    task = json.loads(matches[0].group(1))
    required = ["schema_version", "revision", "task_id", "project", "type", "title", "objective"]
    missing = [k for k in required if task.get(k) in (None, "")]
    if missing:
        raise ValueError(f"Task contract missing: {missing}")
    if int(task["schema_version"]) != 1:
        raise ValueError("Unsupported task schema_version")
    if int(task["revision"]) < 1:
        raise ValueError("revision must be >= 1")

    # condition list validation
    raw_condition = task.get("condition")
    if raw_condition is None:
        task["condition"] = []
    elif isinstance(raw_condition, list):
        task["condition"] = [str(c).strip() for c in raw_condition if str(c).strip()]
    else:
        raise ValueError("condition must be a list of logical issue IDs")

    if "issue_id" in task and task["issue_id"] is not None:
        task["issue_id"] = str(task["issue_id"]).strip()

    return task


def canonical_task_hash(task: dict[str, Any]) -> str:
    payload = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


EVENT_BLOCK = re.compile(r"```orchestrator-event\s*(\{.*?\})\s*```", re.S)
EVENT_HEADER = re.compile(
    r"^\s*\[(?P<issue_tag>issue\d+|[A-Za-z0-9_.-]+)_(?P<event>[A-Za-z0-9_]+)_by(?P<actor>[A-Za-z0-9_]+)\]",
    re.M,
)


def _normalize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    event = str(out.get("event") or out.get("type") or out.get("command") or "").lower()
    if event in ("review_pass", "gpt_approved"):
        out["command"] = "external_review"
        out.setdefault("verdict", "PASS")
    elif event in ("review_fix", "gpt_fix_required"):
        out["command"] = "external_review"
        out.setdefault("verdict", "FIX_REQUIRED")
    elif event == "gate_approved":
        out["command"] = "approve_gate"
    elif event:
        out.setdefault("command", event)

    if "pr" in out and "pr_number" not in out:
        out["pr_number"] = out["pr"]
    if "head_sha" in out and "pr_head_sha" not in out:
        out["pr_head_sha"] = out["head_sha"]
    return out


def _parse_text_event_comment(body: str) -> dict[str, Any] | None:
    match = EVENT_HEADER.search(body or "")
    if not match:
        return None
    issue_tag = match.group("issue_tag")
    event = match.group("event")
    actor = match.group("actor")
    out: dict[str, Any] = {
        "issue_id": issue_tag,
        "event": event,
        "actor": actor,
    }
    for line in (body or "").splitlines():
        line = line.strip()
        if ":" in line:
            key, val = line.split(":", 1)
            k = key.strip().lower()
            v = val.strip()
            if k in ("revision", "review_cycle", "pr", "pr_number", "priority"):
                try:
                    out[k] = int(v)
                except ValueError:
                    out[k] = v
            elif k in ("question_id", "gate_id", "artifact_digest", "head_sha", "pr_head_sha", "reason", "verdict", "answer"):
                out[k] = v

    if "review_pass" in event:
        out["verdict"] = "PASS"
    elif "review_fix" in event:
        out["verdict"] = "FIX_REQUIRED"

    return _normalize_payload(out)


def iter_commands(body: str):
    found_any = False
    for match in COMMAND_BLOCK.finditer(body or ""):
        found_any = True
        yield _normalize_payload(json.loads(match.group(1)))

    for match in EVENT_BLOCK.finditer(body or ""):
        found_any = True
        yield _normalize_payload(json.loads(match.group(1)))

    if not found_any:
        parsed = _parse_text_event_comment(body)
        if parsed:
            yield parsed


def stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

