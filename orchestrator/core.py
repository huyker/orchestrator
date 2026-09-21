from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASK_BLOCK = re.compile(r"```orchestrator-task\s*(\{.*?\})\s*```", re.S)
COMMAND_BLOCK = re.compile(r"```orchestrator-command\s*(\{.*?\})\s*```", re.S)
EVENT_MARKER = "@@ORCH_EVENT@@ "
REVIEW_MARKER = "@@ORCH_REVIEW@@ "

LABEL_READY = "orch:ready"
LABEL_RUNNING = "orch:running"
LABEL_QUESTION = "orch:question"
LABEL_REWORK = "orch:rework"
LABEL_GPT_REVIEW = "orch:gpt-review"
LABEL_APPROVED = "orch:approved"
LABEL_DONE = "orch:done"
LABEL_BLOCKED = "orch:blocked"

ALL_LABELS = {
    LABEL_READY, LABEL_RUNNING, LABEL_QUESTION, LABEL_REWORK,
    LABEL_GPT_REVIEW, LABEL_APPROVED, LABEL_DONE, LABEL_BLOCKED,
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
    allowed_authors: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        control_repo = os.getenv("ORCH_CONTROL_REPO", "").strip()
        token = os.getenv("GITHUB_TOKEN", "").strip()
        if "/" not in control_repo:
            raise ValueError("ORCH_CONTROL_REPO must be OWNER/REPO")
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        runtime = Path(os.getenv("ORCH_RUNTIME_DIR", ".orchestrator-runtime")).resolve()
        raw_authors = os.getenv("ORCH_ALLOWED_AUTHORS", control_repo.split("/", 1)[0])
        return cls(
            control_repo=control_repo,
            token=token,
            registry_file=Path(os.getenv("ORCH_PROJECT_REGISTRY", "projects.json")).resolve(),
            runtime_dir=runtime,
            workspace_root=Path(os.getenv("ORCH_WORKSPACE_ROOT", runtime / "repos")).resolve(),
            poll_interval=max(2, int(os.getenv("ORCH_POLL_INTERVAL", "5"))),
            git_transport=os.getenv("ORCH_GIT_TRANSPORT", "ssh").strip().lower(),
            agy_bin=os.getenv("ORCH_AGY_BIN", "agy").strip(),
            agent_effort=os.getenv("ORCH_AGENT_EFFORT", "medium").strip(),
            agent_timeout=max(30, int(os.getenv("ORCH_AGENT_TIMEOUT", "1800"))),
            allowed_authors=tuple(x.strip() for x in raw_authors.split(",") if x.strip()),
        )


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def _request(self, method: str, path: str, data: Any | None = None) -> Any:
        url = "https://api.github.com" + path
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"GitHub {method} {path}: HTTP {e.code}: {detail}") from e

    def list_ready_issues(self, repo: str) -> list[dict]:
        q = urllib.parse.urlencode({"state": "open", "labels": LABEL_READY, "per_page": 100})
        rows = self._request("GET", f"/repos/{repo}/issues?{q}") or []
        return [x for x in rows if "pull_request" not in x]

    def get_issue(self, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{repo}/issues/{number}")

    def comments(self, repo: str, number: int) -> list[dict]:
        return self._request("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100") or []

    def comment(self, repo: str, number: int, body: str) -> dict:
        return self._request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def set_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._request("PUT", f"/repos/{repo}/issues/{number}/labels", {"labels": labels})

    def create_pr(self, repo: str, title: str, body: str, head: str, base: str) -> dict:
        return self._request("POST", f"/repos/{repo}/pulls", {
            "title": title, "body": body, "head": head, "base": base
        })

    def find_open_pr(self, repo: str, head: str, base: str) -> dict | None:
        owner = repo.split("/", 1)[0]
        q = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{head}", "base": base})
        rows = self._request("GET", f"/repos/{repo}/pulls?{q}") or []
        return rows[0] if rows else None

    def get_pr(self, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{repo}/pulls/{number}")


class Registry:
    def __init__(self, path: Path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1 or not isinstance(raw.get("projects"), list):
            raise ValueError("Invalid projects.json")
        self.projects = {x["id"]: x for x in raw["projects"] if x.get("enabled", True)}

    def resolve(self, project: str) -> dict:
        item = self.projects.get(project)
        if not item:
            raise ValueError(f"Unknown/disabled project: {project}")
        if "/" not in str(item.get("repo", "")):
            raise ValueError(f"Invalid repo for project {project}")
        return item

    def list(self) -> list[dict]:
        return [self.projects[k] for k in sorted(self.projects)]


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS active (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL)")
        self.db.commit()

    def get(self) -> dict | None:
        row = self.db.execute("SELECT payload FROM active WHERE singleton=1").fetchone()
        return json.loads(row[0]) if row else None

    def set(self, payload: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO active(singleton,payload) VALUES(1,?)", (json.dumps(payload),))
        self.db.commit()

    def clear(self) -> None:
        self.db.execute("DELETE FROM active WHERE singleton=1")
        self.db.commit()


def parse_task(body: str) -> dict:
    m = TASK_BLOCK.search(body or "")
    if not m:
        raise ValueError("Issue body must contain one orchestrator-task JSON block")
    task = json.loads(m.group(1))
    required = ["schema_version", "revision", "task_id", "project", "type", "title", "objective"]
    missing = [k for k in required if task.get(k) in (None, "")]
    if missing:
        raise ValueError(f"Task contract missing: {missing}")
    if int(task["schema_version"]) != 1:
        raise ValueError("Unsupported task schema_version")
    return task


def latest_command(comments: list[dict], command: str, revision: int, allowed: tuple[str, ...]) -> dict | None:
    for item in reversed(comments):
        login = ((item.get("user") or {}).get("login") or "")
        if login not in allowed:
            continue
        for m in COMMAND_BLOCK.finditer(item.get("body") or ""):
            payload = json.loads(m.group(1))
            if payload.get("command") == command and int(payload.get("revision", -1)) == revision:
                return payload
    return None


def load_catalog(root: Path, manifest_path: str) -> dict:
    manifest = json.loads((root / manifest_path).read_text(encoding="utf-8"))
    if int(manifest.get("schema_version", 0)) not in (1, 2):
        raise ValueError("Unsupported project manifest")
    agents_dir = root / manifest.get("agent_profiles_dir", ".orchestrator/agents")
    tasks_dir = root / manifest.get("task_profiles_dir", ".orchestrator/tasks")
    agents = {}
    tasks = {}
    if agents_dir.is_dir():
        for p in sorted(agents_dir.glob("*.json")):
            row = json.loads(p.read_text(encoding="utf-8"))
            agents[row.get("id", p.stem)] = row
    if tasks_dir.is_dir():
        for p in sorted(tasks_dir.glob("*.json")):
            row = json.loads(p.read_text(encoding="utf-8"))
            tasks[row.get("id", p.stem)] = row
    plans = []
    for pattern in manifest.get("plan_globs", []):
        plans.extend(x.relative_to(root).as_posix() for x in root.glob(pattern) if x.is_file())
    return {"manifest": manifest, "agents": agents, "tasks": tasks, "plans": sorted(set(plans))}


class Workspace:
    def __init__(self, settings: Settings):
        self.s = settings

    def _run(self, args: list[str], cwd: Path | None = None, timeout: int = 120) -> str:
        p = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        if p.returncode:
            raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(args)}\n{p.stdout}")
        return p.stdout.strip()

    def clone_url(self, repo: str) -> str:
        return f"git@github.com:{repo}.git" if self.s.git_transport == "ssh" else f"https://github.com/{repo}.git"

    def prepare(self, repo: str, base: str, issue_number: int, task_id: str) -> tuple[Path, str]:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", task_id).strip("-").lower()
        repo_dir = self.s.workspace_root / repo.replace("/", "__")
        if not repo_dir.exists():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            self._run(["git", "clone", self.clone_url(repo), str(repo_dir)], timeout=300)
        self._run(["git", "fetch", "origin", base], cwd=repo_dir)
        branch = f"task/issue-{issue_number}-{safe}"
        wt = self.s.runtime_dir / "worktrees" / f"{repo.replace('/', '__')}__{issue_number}"
        if wt.exists():
            return wt, branch
        wt.parent.mkdir(parents=True, exist_ok=True)
        local = self._run(["git", "branch", "--list", branch], cwd=repo_dir)
        if local:
            self._run(["git", "worktree", "add", str(wt), branch], cwd=repo_dir)
        else:
            self._run(["git", "worktree", "add", "-b", branch, str(wt), f"origin/{base}"], cwd=repo_dir)
        return wt, branch

    def changed_files(self, wt: Path, base: str) -> list[str]:
        tracked = self._run(["git", "diff", "--name-only", f"origin/{base}...HEAD"], cwd=wt).splitlines()
        unstaged = self._run(["git", "diff", "--name-only"], cwd=wt).splitlines()
        untracked = self._run(["git", "ls-files", "--others", "--exclude-standard"], cwd=wt).splitlines()
        return sorted(set(x for x in tracked + unstaged + untracked if x.strip()))

    def diff(self, wt: Path, base: str) -> str:
        self._run(["git", "add", "-N", "."], cwd=wt)
        return self._run(["git", "diff", f"origin/{base}", "--"], cwd=wt)

    def commit_push(self, wt: Path, branch: str, message: str) -> None:
        self._run(["git", "add", "."], cwd=wt)
        status = self._run(["git", "status", "--porcelain"], cwd=wt)
        if status:
            self._run(["git", "commit", "-m", message], cwd=wt)
        self._run(["git", "push", "-u", "origin", branch], cwd=wt, timeout=300)


class IssueOrchestrator:
    def __init__(self, settings: Settings):
        self.s = settings
        self.github = GitHub(settings.token)
        self.registry = Registry(settings.registry_file)
        self.state = State(settings.runtime_dir / "state.sqlite3")
        self.ws = Workspace(settings)

    def event(self, issue: int, event_type: str, **payload: Any) -> None:
        body = {
            "schema": "orch.event.v1",
            "type": event_type,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **payload,
        }
        self.github.comment(self.s.control_repo, issue, "```orchestrator-event\n" + json.dumps(body, ensure_ascii=False, indent=2) + "\n```")

    def labels(self, issue: int, wanted: str) -> None:
        self.github.set_labels(self.s.control_repo, issue, [wanted])

    def resolve_profile(self, task: dict, catalog: dict) -> tuple[dict | None, dict, dict]:
        profiles = catalog["tasks"]
        profile = None
        explicit = task.get("task_profile")
        if explicit:
            profile = profiles.get(explicit)
            if not profile:
                raise ValueError(f"Unknown task_profile: {explicit}")
        else:
            matches = [x for x in profiles.values() if task["type"] in x.get("task_types", [])]
            if len(matches) == 1:
                profile = matches[0]
            elif len(matches) > 1:
                raise ValueError("Multiple task profiles match; set task_profile in Issue")
        exec_id = task.get("executor_profile") or (profile or {}).get("executor_profile")
        rev_id = task.get("reviewer_profile") or (profile or {}).get("reviewer_profile")
        executor = catalog["agents"].get(exec_id)
        reviewer = catalog["agents"].get(rev_id)
        if not executor or executor.get("role") != "executor":
            raise ValueError(f"Invalid executor profile: {exec_id}")
        if not reviewer or reviewer.get("role") != "reviewer":
            raise ValueError(f"Invalid reviewer profile: {rev_id}")
        return profile, executor, reviewer

    def build_context(self, wt: Path, task: dict, catalog: dict, profile: dict | None, agent: dict) -> str:
        files = list(catalog["manifest"].get("context_files", []))
        files += list(task.get("plan_refs", []))
        files += list((profile or {}).get("context_files", []))
        files += list(agent.get("context_files", [])) + list(agent.get("config_files", []))
        chunks = []
        seen = set()
        for rel in files:
            if rel in seen:
                continue
            seen.add(rel)
            p = wt / rel
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                chunks.append(f"### {rel}\n{text[:50000]}")
        return "\n\n".join(chunks)

    def run_agent(self, agent: dict, prompt: str, wt: Path) -> tuple[int, str]:
        binary = shutil.which(self.s.agy_bin)
        if not binary:
            return 127, f"AGY executable not found: {self.s.agy_bin}"
        cmd = [binary, "--print", prompt, "--agent", agent["agy_agent"], "--effort", agent.get("effort") or self.s.agent_effort]
        p = subprocess.run(cmd, cwd=wt, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=self.s.agent_timeout)
        return p.returncode, p.stdout

    def machine_acceptance(self, task: dict, catalog: dict, profile: dict | None, wt: Path) -> dict:
        checks = dict(task.get("checks") or {})
        for key in ("required_paths", "forbidden_paths", "required_diff_globs", "forbidden_diff_globs", "test_profiles"):
            checks[key] = list((profile or {}).get(key, [])) + list(checks.get(key, []))
        changed = self.ws.changed_files(wt, task["base_branch"])
        failures = []
        for rel in checks.get("required_paths", []):
            if not (wt / rel).exists():
                failures.append(f"missing required path: {rel}")
        for pat in checks.get("forbidden_paths", []):
            if any(fnmatch.fnmatch(x, pat) for x in changed):
                failures.append(f"forbidden path changed: {pat}")
        for pat in checks.get("required_diff_globs", []):
            if not any(fnmatch.fnmatch(x, pat) for x in changed):
                failures.append(f"required diff glob missing: {pat}")
        for pat in checks.get("forbidden_diff_globs", []):
            if any(fnmatch.fnmatch(x, pat) for x in changed):
                failures.append(f"forbidden diff glob changed: {pat}")
        if checks.get("require_substantive_diff", (profile or {}).get("require_substantive_diff", True)) and not changed:
            failures.append("no substantive diff")
        tests = {}
        configured = catalog["manifest"].get("test_profiles", {})
        for name in checks.get("test_profiles", []):
            commands = configured.get(name)
            if not commands:
                failures.append(f"unknown test profile: {name}")
                continue
            outputs = []
            for command in commands:
                p = subprocess.run(command, cwd=wt, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                outputs.append({"command": command, "exit_code": p.returncode, "output": p.stdout[-8000:]})
                if p.returncode:
                    failures.append(f"test profile {name} failed: {command}")
                    break
            tests[name] = outputs
        return {"pass": not failures, "changed_files": changed, "failures": failures, "tests": tests}

    def reviewer_result(self, output: str, task: dict) -> dict:
        rows = [x for x in output.splitlines() if x.startswith(REVIEW_MARKER)]
        if len(rows) != 1:
            raise ValueError("Reviewer must emit exactly one @@ORCH_REVIEW@@ record")
        review = json.loads(rows[0][len(REVIEW_MARKER):])
        expected_a = list(task.get("acceptance", []))
        expected_p = list(task.get("prohibited", []))
        amap = {x.get("criterion"): x for x in review.get("acceptance", [])}
        pmap = {x.get("rule"): x for x in review.get("prohibited", [])}
        errors = []
        for x in expected_a:
            row = amap.get(x)
            if not row or row.get("status") != "PASS" or not str(row.get("evidence", "")).strip():
                errors.append(f"acceptance not proven: {x}")
        for x in expected_p:
            row = pmap.get(x)
            if not row or row.get("status") != "PASS" or not str(row.get("evidence", "")).strip():
                errors.append(f"prohibited rule not proven: {x}")
        min_score = int((task.get("review") or {}).get("min_score", 100))
        if review.get("verdict") != "PASS" or int(review.get("score", 0)) < min_score:
            errors.append("review verdict/score failed")
        if errors:
            raise ValueError("; ".join(errors))
        return review

    def process(self, issue: dict) -> None:
        number = int(issue["number"])
        author = ((issue.get("user") or {}).get("login") or "")
        if author not in self.s.allowed_authors:
            self.labels(number, LABEL_BLOCKED)
            self.event(number, "blocked", reason=f"unauthorized issue author: {author}")
            return

        task = parse_task(issue.get("body") or "")
        project = self.registry.resolve(task["project"])
        if task.get("target_repo") and task["target_repo"] != project["repo"]:
            raise ValueError("Issue target_repo does not match registry")
        task["target_repo"] = project["repo"]
        task["base_branch"] = task.get("base_branch") or project.get("default_branch", "main")

        wt, branch = self.ws.prepare(task["target_repo"], task["base_branch"], number, task["task_id"])
        catalog = load_catalog(wt, project.get("manifest_path", ".orchestrator/project.json"))
        manifest = catalog["manifest"]
        if manifest.get("project") != task["project"] or manifest.get("repository") != task["target_repo"]:
            raise ValueError("Project manifest identity mismatch")
        profile, executor, reviewer = self.resolve_profile(task, catalog)

        active = {
            "issue_number": number, "task_id": task["task_id"], "revision": int(task["revision"]),
            "project": task["project"], "target_repo": task["target_repo"], "branch": branch,
            "worktree": str(wt), "status": "RUNNING", "pr_number": None,
        }
        self.state.set(active)
        self.labels(number, LABEL_RUNNING)
        self.event(number, "started", project=task["project"], repo=task["target_repo"], branch=branch,
                   task_profile=(profile or {}).get("id"), executor_profile=executor.get("id"),
                   reviewer_profile=reviewer.get("id"), available_plans=catalog["plans"])

        comments = self.github.comments(self.s.control_repo, number)
        context = self.build_context(wt, task, catalog, profile, executor)
        prompt = f"""You are the LOCAL EXECUTOR for GitHub Issue #{number}.
The Issue contract is authoritative. Do not read legacy task/status files as task transport.
Task: {json.dumps(task, ensure_ascii=False, indent=2)}
Project task profile: {json.dumps(profile, ensure_ascii=False, indent=2)}
Project executor profile: {json.dumps(executor, ensure_ascii=False, indent=2)}
Project context:
{context}

Rules:
- Implement only this Issue contract.
- Do not commit/push; orchestrator owns Git lifecycle.
- If a decision is required, emit exactly one line:
  @@ORCH_EVENT@@ {{"type":"question","question_id":"stable-id","message":"...","options":[]}}
- Otherwise finish implementation and local validation.
"""
        code, output = self.run_agent(executor, prompt, wt)
        question = None
        for line in output.splitlines():
            if line.startswith(EVENT_MARKER):
                evt = json.loads(line[len(EVENT_MARKER):])
                if evt.get("type") == "question":
                    question = evt
                    break
        if question:
            active["status"] = "WAITING_ANSWER"
            self.state.set(active)
            self.labels(number, LABEL_QUESTION)
            self.event(number, "question", **question)
            return
        if code:
            raise RuntimeError(f"executor failed: {output[-4000:]}")

        first = self.machine_acceptance(task, catalog, profile, wt)
        if not first["pass"]:
            active["status"] = "REWORK"
            self.state.set(active)
            self.labels(number, LABEL_REWORK)
            self.event(number, "acceptance_failed", report=first)
            return

        diff = self.ws.diff(wt, task["base_branch"])
        reviewer_context = self.build_context(wt, task, catalog, profile, reviewer)
        rprompt = f"""You are an INDEPENDENT REVIEWER for GitHub Issue #{number}.
You did not implement the task. The Issue contract is authoritative.
Task: {json.dumps(task, ensure_ascii=False, indent=2)}
Project reviewer profile: {json.dumps(reviewer, ensure_ascii=False, indent=2)}
Project context:
{reviewer_context}
Deterministic acceptance: {json.dumps(first, ensure_ascii=False, indent=2)}
Actual diff:
{diff}

Inspect actual files/assets/tests. End stdout with exactly one line:
@@ORCH_REVIEW@@ {{"verdict":"PASS|FAIL","score":0,"summary":"...","findings":[],"acceptance":[{{"criterion":"exact acceptance string","status":"PASS|FAIL","evidence":"exact evidence"}}],"prohibited":[{{"rule":"exact prohibited string","status":"PASS|FAIL","evidence":"exact evidence"}}],"risks":[]}}
Include exactly one entry for every acceptance/prohibited string. Evidence may not be empty.
"""
        rcode, rout = self.run_agent(reviewer, rprompt, wt)
        if rcode:
            raise RuntimeError(f"reviewer failed: {rout[-4000:]}")
        review = self.reviewer_result(rout, task)

        final = self.machine_acceptance(task, catalog, profile, wt)
        if not final["pass"]:
            raise RuntimeError(f"final acceptance failed: {final['failures']}")

        self.ws.commit_push(wt, branch, f"task({task['task_id']}): issue #{number}")
        pr = self.github.find_open_pr(task["target_repo"], branch, task["base_branch"])
        if not pr:
            pr = self.github.create_pr(
                task["target_repo"],
                f"{task['task_id']}: {task['title']}",
                f"Implements {self.s.control_repo}#{number}.\n\nReview contract and Q&A live in the Issue.",
                branch,
                task["base_branch"],
            )
        active["status"] = "WAITING_GPT_REVIEW"
        active["pr_number"] = pr["number"]
        self.state.set(active)
        self.labels(number, LABEL_GPT_REVIEW)
        self.event(number, "ready_for_gpt_review", pr_url=pr["html_url"], review=review, acceptance=final)

    def handle_active(self, active: dict) -> None:
        issue = self.github.get_issue(self.s.control_repo, active["issue_number"])
        comments = self.github.comments(self.s.control_repo, active["issue_number"])
        revision = int(active["revision"])

        if active["status"] == "WAITING_ANSWER":
            cmd = latest_command(comments, "answer", revision, self.s.allowed_authors)
            if cmd:
                # Requeue same Issue; executor receives Issue discussion on next pass in v0.3.
                self.state.clear()
                self.labels(active["issue_number"], LABEL_READY)
                self.event(active["issue_number"], "answer_received", answer=cmd.get("answer"))
            return

        if active["status"] == "REWORK":
            cmd = latest_command(comments, "retry", revision, self.s.allowed_authors)
            if cmd:
                self.state.clear()
                self.labels(active["issue_number"], LABEL_READY)
                self.event(active["issue_number"], "retry_accepted")
            return

        if active["status"] == "WAITING_GPT_REVIEW":
            cmd = latest_command(comments, "external_review", revision, self.s.allowed_authors)
            if not cmd:
                return
            if cmd.get("verdict") == "FIX_REQUIRED":
                self.state.clear()
                self.labels(active["issue_number"], LABEL_READY)
                self.event(active["issue_number"], "gpt_fix_required", findings=cmd.get("findings", []))
                return
            if cmd.get("verdict") == "PASS":
                active["status"] = "APPROVED_WAITING_MERGE"
                self.state.set(active)
                self.labels(active["issue_number"], LABEL_APPROVED)
                self.event(active["issue_number"], "gpt_approved", pr_number=active["pr_number"])
            return

        if active["status"] == "APPROVED_WAITING_MERGE":
            pr = self.github.get_pr(active["target_repo"], int(active["pr_number"]))
            if pr.get("merged_at"):
                self.labels(active["issue_number"], LABEL_DONE)
                self.event(active["issue_number"], "complete", merged_pr=pr["html_url"])
                self.github._request("PATCH", f"/repos/{self.s.control_repo}/issues/{active['issue_number']}", {"state": "closed"})
                self.state.clear()

    def tick(self) -> None:
        active = self.state.get()
        if active:
            self.handle_active(active)
            return
        issues = self.github.list_ready_issues(self.s.control_repo)
        if not issues:
            return
        def sort_key(issue: dict) -> tuple[int, int]:
            try:
                task = parse_task(issue.get("body") or "")
                return int(task.get("priority", 9)), int(issue["number"])
            except Exception:
                return 99, int(issue["number"])
        for issue in sorted(issues, key=sort_key):
            try:
                self.process(issue)
                return
            except Exception as exc:
                self.labels(int(issue["number"]), LABEL_BLOCKED)
                self.event(int(issue["number"]), "blocked", reason=str(exc))
                self.state.clear()
                return

    def serve(self) -> None:
        self.s.runtime_dir.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self.tick()
            except Exception as exc:
                print(f"[orchestrator] tick error: {exc}", flush=True)
            time.sleep(self.s.poll_interval)
