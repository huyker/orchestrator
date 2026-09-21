from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .models import ALL_LABELS, LABEL_READY


class GitHubClient:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, data: Any | None = None) -> Any:
        url = "https://api.github.com" + path
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"GitHub {method} {path}: HTTP {exc.code}: {detail}") from exc

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
            if "HTTP 404" not in str(exc):
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

    def post_command(self, repo: str, issue: int, payload: dict[str, Any]) -> dict:
        block = "```orchestrator-command\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        return self.comment(repo, issue, block)
