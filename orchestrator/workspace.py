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
            encoding="utf-8",
            errors="replace",
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
        owner, name = repo.split("/", 1)
        return (self.settings.workspace_root / owner / name).resolve()

    def remote_default_branch(self, root: Path) -> str:
        proc = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if proc.returncode == 0 and proc.stdout.strip().startswith("origin/"):
            return proc.stdout.strip().split("/", 1)[1]

        proc = subprocess.run(
            ["git", "remote", "show", "origin"],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if "HEAD branch:" in line:
                    value = line.split("HEAD branch:", 1)[1].strip()
                    if value and value != "(unknown)":
                        return value

        branch = self.inspect_repo(root)["branch"]
        if branch != "(detached)":
            return str(branch)
        raise RuntimeError(f"Cannot determine default branch for {root}")

    def inspect_repo(self, root: Path) -> dict[str, str | bool]:
        top = Path(self.run(["git", "rev-parse", "--show-toplevel"], cwd=root)).resolve()
        git_dir_raw = self.run(["git", "rev-parse", "--git-dir"], cwd=top)
        git_dir = Path(git_dir_raw)
        if not git_dir.is_absolute():
            git_dir = (top / git_dir).resolve()
        branch_proc = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=top,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else "(detached)"
        remote_proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=top,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return {
            "root": str(top),
            "git_dir": str(git_dir),
            "head": self.run(["git", "rev-parse", "HEAD"], cwd=top),
            "branch": branch,
            "origin": remote_proc.stdout.strip() if remote_proc.returncode == 0 else "",
            "dirty": bool(self.run(["git", "status", "--porcelain"], cwd=top)),
        }

    def sync_project(
        self,
        repo: str,
        default_branch: str | None = None,
    ) -> Path:
        root = self.repo_dir(repo)
        if not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            self.run(["git", "clone", self.clone_url(repo), str(root)], timeout=300)
        elif not (root / ".git").exists():
            raise RuntimeError(f"Managed project path exists but is not a Git repository: {root}")

        expected_origin = self.clone_url(repo)
        actual_origin = self.run(["git", "remote", "get-url", "origin"], cwd=root)
        if actual_origin != expected_origin:
            normalized_actual = (
                actual_origin.removesuffix(".git")
                .replace("https://github.com/", "")
                .replace("git@github.com:", "")
            )
            if normalized_actual != repo:
                raise RuntimeError(
                    f"Managed path {root} belongs to {actual_origin}, expected {repo}"
                )

        self.run(["git", "fetch", "origin", "--prune"], cwd=root, timeout=300)
        branch = (default_branch or "").strip() or self.remote_default_branch(root)

        remote_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=root,
        ).returncode == 0
        if not remote_exists:
            branch = self.remote_default_branch(root)

        self.run(["git", "checkout", branch], cwd=root)
        try:
            self.run(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=root, timeout=300)
        except Exception:
            self.run(["git", "pull", "--ff-only", "origin", branch], cwd=root, timeout=300)
        return root

    def prepare_task(
        self,
        repo: str,
        base: str,
        issue_number: int,
        task_id: str,
    ) -> tuple[Path, str]:
        root = self.repo_dir(repo)
        if not root.exists():
            self.sync_project(repo, base)
        else:
            self.sync_project(repo, base)

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
