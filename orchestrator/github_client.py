from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
from typing import Any

from .models import ALL_LABELS, LABEL_READY


class GitHubClient:
    """GitHub API client backed by the already-authenticated local gh CLI."""

    def __init__(self, token: str = ""):
        self.token = str(token or "").strip()
        self._auth_cache: dict[str, Any] | None = None
        self._auth_cache_time: float = 0.0
        self._auth_cache_ttl: float = 60.0

    def _get_token(self) -> str:
        import os
        token = (self.token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
        if token:
            return token
        if hasattr(self, "_cached_token") and self._cached_token:
            return self._cached_token

        # 1. Fallback to gh auth token first (fast and reliable)
        if shutil.which("gh"):
            try:
                proc = subprocess.run(
                    ["gh", "auth", "token"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    self._cached_token = proc.stdout.strip()
                    return self._cached_token
            except Exception:
                pass

        # 2. Fallback to git config github.token
        try:
            proc = subprocess.run(
                ["git", "config", "--get", "github.token"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                self._cached_token = proc.stdout.strip()
                return self._cached_token
        except Exception:
            pass

        # 3. Fallback to git credential helper (strictly non-interactive)
        try:
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="")
            proc = subprocess.run(
                ["git", "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True,
                text=True,
                timeout=3,
                env=env,
            )
            if proc.returncode == 0 and proc.stdout:
                for line in proc.stdout.splitlines():
                    if line.startswith("password="):
                        val = line.split("=", 1)[1].strip()
                        if val:
                            self._cached_token = val
                            return self._cached_token
        except Exception:
            pass

        return ""

    def auth_status(self, force: bool = False) -> dict[str, Any]:
        import time
        now = time.time()
        if not force and self._auth_cache is not None and (now - self._auth_cache_time < self._auth_cache_ttl):
            return dict(self._auth_cache)

        res: dict[str, Any]
        if shutil.which("gh"):
            try:
                user = self._request_gh("GET", "/user")
                res = {
                    "connected": True,
                    "login": user.get("login"),
                    "name": user.get("name"),
                    "error": None,
                    "provider": "gh-cli",
                }
                self._auth_cache = res
                self._auth_cache_time = now
                self._auth_cache_ttl = 60.0
                return dict(res)
            except Exception as exc:
                token = self._get_token()
                if not token:
                    res = {
                        "connected": False,
                        "login": None,
                        "error": f"GitHub CLI is not authenticated: {exc}",
                        "provider": "gh-cli",
                    }
                    self._auth_cache = res
                    self._auth_cache_time = now
                    self._auth_cache_ttl = 30.0
                    return dict(res)
        else:
            token = self._get_token()

        if token:
            try:
                user = self._request_token("GET", "/user", token=token)
                res = {
                    "connected": True,
                    "login": user.get("login"),
                    "name": user.get("name"),
                    "error": None,
                    "provider": "token",
                }
                self._auth_cache = res
                self._auth_cache_time = now
                self._auth_cache_ttl = 60.0
                return dict(res)
            except Exception as exc:
                res = {
                    "connected": False,
                    "login": None,
                    "error": f"GitHub Token authentication failed: {exc}",
                    "provider": "token",
                }
                self._auth_cache = res
                self._auth_cache_time = now
                self._auth_cache_ttl = 60.0
                return dict(res)
        res = {
            "connected": False,
            "login": None,
            "error": "GitHub CLI is not authenticated and no GITHUB_TOKEN provided",
            "provider": "gh-cli",
        }
        self._auth_cache = res
        self._auth_cache_time = now
        self._auth_cache_ttl = 30.0
        return dict(res)

    def _request_gh(self, method: str, path: str, data: Any | None = None) -> Any:
        endpoint = path.lstrip("/")
        args = ["gh", "api", endpoint, "-X", method]
        input_text = None
        if data is not None:
            args.extend(["--input", "-"])
            input_text = json.dumps(data, ensure_ascii=False)

        proc = subprocess.run(
            args,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        if proc.returncode:
            raise RuntimeError(f"gh api {method} {path}: {proc.stdout.strip()}")
        raw = proc.stdout.strip()
        return json.loads(raw) if raw else None

    def _request_token(self, method: str, path: str, data: Any | None = None, token: str = "") -> Any:
        import urllib.request
        url = f"https://api.github.com/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "orchestrator",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        req_data = None
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            req_data = json.dumps(data, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None

    def request(self, method: str, path: str, data: Any | None = None) -> Any:
        if shutil.which("gh"):
            try:
                return self._request_gh(method, path, data)
            except Exception as exc:
                token = self._get_token()
                if not token:
                    raise
        else:
            token = self._get_token()
        if token:
            return self._request_token(method, path, data, token=token)
        raise RuntimeError("GitHub CLI (gh) or GITHUB_TOKEN is required for Issue/PR operations")

    def paged(self, path: str, per_page: int = 100) -> list[dict]:
        sep = "&" if "?" in path else "?"
        rows: list[dict] = []
        page = 1
        while True:
            batch = self.request("GET", f"{path}{sep}per_page={per_page}&page={page}") or []
            rows.extend(batch)
            if len(batch) < per_page:
                return rows
            page += 1

    def list_ready_issues(self, repo: str) -> list[dict]:
        label = urllib.parse.quote(LABEL_READY, safe="")
        rows = self.paged(f"/repos/{repo}/issues?state=open&labels={label}")
        return [row for row in rows if "pull_request" not in row]

    def list_open_orchestrator_issues(self, repo: str) -> list[dict]:
        rows = self.paged(f"/repos/{repo}/issues?state=open")
        return [row for row in rows if "pull_request" not in row]

    def list_all_orchestrator_issues(self, repo: str) -> list[dict]:
        rows = self.paged(f"/repos/{repo}/issues?state=all")
        return [row for row in rows if "pull_request" not in row]

    def get_issue(self, repo: str, number: int) -> dict:
        return self.request("GET", f"/repos/{repo}/issues/{number}")

    def comments(self, repo: str, number: int) -> list[dict]:
        return self.paged(f"/repos/{repo}/issues/{number}/comments")

    def comment(self, repo: str, number: int, body: str) -> dict:
        return self.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def close_issue(self, repo: str, number: int) -> None:
        self.request("PATCH", f"/repos/{repo}/issues/{number}", {"state": "closed"})

    def set_lifecycle_label(self, repo: str, number: int, wanted: str) -> None:
        issue = self.get_issue(repo, number)
        current = [x["name"] for x in issue.get("labels", [])]
        preserved = [x for x in current if x not in ALL_LABELS and not x.startswith("orch:")]
        self.request("PUT", f"/repos/{repo}/issues/{number}/labels", {"labels": preserved + [wanted]})

    def ensure_label(self, repo: str, name: str) -> None:
        encoded = urllib.parse.quote(name, safe="")
        try:
            self.request("GET", f"/repos/{repo}/labels/{encoded}")
            return
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise
        self.request("POST", f"/repos/{repo}/labels", {
            "name": name,
            "color": "1f6feb",
            "description": "Managed by Issue Orchestrator",
        })

    def ensure_labels(self, repo: str) -> None:
        for name in sorted(ALL_LABELS):
            self.ensure_label(repo, name)

    def find_open_pr(self, repo: str, head: str, base: str) -> dict | None:
        owner = repo.split("/", 1)[0]
        q = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{head}", "base": base})
        rows = self.request("GET", f"/repos/{repo}/pulls?{q}") or []
        return rows[0] if rows else None

    def create_pr(self, repo: str, title: str, body: str, head: str, base: str) -> dict:
        return self.request("POST", f"/repos/{repo}/pulls", {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        })

    def get_pr(self, repo: str, number: int) -> dict:
        return self.request("GET", f"/repos/{repo}/pulls/{number}")

    def merge_pr(self, repo: str, number: int, commit_title: str = "", merge_method: str = "merge") -> dict:
        data: dict[str, Any] = {"merge_method": merge_method}
        if commit_title:
            data["commit_title"] = commit_title
        return self.request("PUT", f"/repos/{repo}/pulls/{number}/merge", data)

    def post_command(self, repo: str, issue: int, payload: dict[str, Any]) -> dict:
        block = "```orchestrator-command\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        return self.comment(repo, issue, block)
