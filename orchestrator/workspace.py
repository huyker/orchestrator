from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from .models import Settings


class WorkspaceManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, args: list[str], cwd: Path | None = None, timeout: int = 120) -> str:
        proc = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        if proc.returncode:
            raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(args)}\n{proc.stdout}")
        return proc.stdout.strip()

    def clone_url(self, repo: str) -> str:
        if self.settings.git_transport == "ssh":
            return f"git@github.com:{repo}.git"
        return f"https://github.com/{repo}.git"

    def repo_dir(self, repo: str) -> Path:
        return self.settings.workspace_root / repo.replace("/", "__")

    def sync_project(self, repo: str, default_branch: str) -> Path:
        root = self.repo_dir(repo)
        if not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            self.run(["git", "clone", self.clone_url(repo), str(root)], timeout=300)
        self.run(["git", "fetch", "origin", "--prune"], cwd=root, timeout=300)
        self.run(["git", "checkout", default_branch], cwd=root)
        self.run(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=root)
        return root

    def prepare_task(self, repo: str, base: str, issue_number: int, task_id: str) -> tuple[Path, str]:
        root = self.repo_dir(repo)
        if not root.exists():
            self.sync_project(repo, base)
        else:
            self.run(["git", "fetch", "origin", "--prune"], cwd=root, timeout=300)

        safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", task_id).strip("-").lower() or "task"
        branch = f"task/issue-{issue_number}-{safe}"
        worktree = self.settings.runtime_dir / "worktrees" / f"{repo.replace('/', '__')}__{issue_number}"
        if worktree.exists():
            return worktree, branch
        worktree.parent.mkdir(parents=True, exist_ok=True)

        local_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root
        ).returncode == 0
        remote_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd=root
        ).returncode == 0

        if local_exists:
            self.run(["git", "worktree", "add", str(worktree), branch], cwd=root)
        elif remote_exists:
            self.run(["git", "branch", branch, f"origin/{branch}"], cwd=root)
            self.run(["git", "worktree", "add", str(worktree), branch], cwd=root)
        else:
            self.run(["git", "worktree", "add", "-b", branch, str(worktree), f"origin/{base}"], cwd=root)
        return worktree, branch

    def changed_files(self, worktree: Path, base: str) -> list[str]:
        committed = self.run(["git", "diff", "--name-only", f"origin/{base}...HEAD"], cwd=worktree).splitlines()
        unstaged = self.run(["git", "diff", "--name-only"], cwd=worktree).splitlines()
        staged = self.run(["git", "diff", "--cached", "--name-only"], cwd=worktree).splitlines()
        untracked = self.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=worktree).splitlines()
        return sorted(set(x for x in committed + unstaged + staged + untracked if x.strip()))

    def diff(self, worktree: Path, base: str, max_chars: int = 100_000) -> str:
        self.run(["git", "add", "-N", "."], cwd=worktree)
        raw = self.run(["git", "diff", f"origin/{base}", "--"], cwd=worktree)
        if len(raw) > max_chars:
            return raw[:max_chars] + "\n\n[diff truncated; reviewer must inspect files directly]"
        return raw

    def artifact_digest(self, worktree: Path, base: str) -> str:
        digest = hashlib.sha256()
        for rel in self.changed_files(worktree, base):
            digest.update(rel.encode("utf-8"))
            path = worktree / rel
            if path.is_file():
                with path.open("rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
        return digest.hexdigest()

    def commit_push(self, worktree: Path, branch: str, message: str) -> str:
        self.run(["git", "add", "."], cwd=worktree)
        status = self.run(["git", "status", "--porcelain"], cwd=worktree)
        if status:
            self.run(["git", "commit", "-m", message], cwd=worktree)
        self.run(["git", "push", "-u", "origin", branch], cwd=worktree, timeout=300)
        return self.run(["git", "rev-parse", "HEAD"], cwd=worktree)
