from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


class SelfUpdater:
    """Safe fast-forward updater for the orchestrator's own checkout."""

    def __init__(
        self,
        root: Path,
        *,
        registry_file: Path,
        remote: str = "origin",
        branch: str = "main",
    ):
        self.root = root.resolve()
        self.registry_file = registry_file.resolve()
        self.remote = remote
        self.branch = branch
        self._status: dict[str, Any] = {
            "state": "starting",
            "local_sha": None,
            "remote_sha": None,
            "message": "Self updater not checked yet",
            "checked_at": None,
        }

    def _run(self, args: list[str], *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            args,
            cwd=self.root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        if check and proc.returncode:
            raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(args)}\n{proc.stdout}")
        return proc

    def _git(self, *args: str, check: bool = True, timeout: int = 120) -> str:
        return self._run(["git", *args], check=check, timeout=timeout).stdout.strip()

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def _set(self, state: str, message: str, **extra: Any) -> dict[str, Any]:
        self._status = {
            **self._status,
            "state": state,
            "message": message,
            "checked_at": time.time(),
            **extra,
        }
        return self.status()

    def _registry_rel(self) -> str | None:
        try:
            return self.registry_file.relative_to(self.root).as_posix()
        except ValueError:
            return None

    @staticmethod
    def _merge_registry(base_text: str, local_text: str) -> str:
        base = json.loads(base_text)
        local = json.loads(local_text)
        if base.get("schema_version") != 1 or local.get("schema_version") != 1:
            raise ValueError("Unsupported projects.json schema during self update")
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for item in base.get("projects", []):
            pid = str(item.get("id") or "")
            if pid:
                merged[pid] = item
                order.append(pid)
        for item in local.get("projects", []):
            pid = str(item.get("id") or "")
            if not pid:
                continue
            if pid not in merged:
                order.append(pid)
            # Local runtime registration wins for an existing project id.
            merged[pid] = item
        base["projects"] = [merged[pid] for pid in order]
        return json.dumps(base, ensure_ascii=False, indent=2) + "\n"

    def check_and_apply(self, *, active_task: bool) -> dict[str, Any]:
        try:
            top = Path(self._git("rev-parse", "--show-toplevel")).resolve()
        except Exception as exc:
            return self._set("disabled", f"Orchestrator source is not a Git checkout: {exc}")
        if top != self.root:
            return self._set("blocked", f"Unexpected Git root: {top}")

        branch_proc = self._run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            check=False,
        )
        current_branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else "(detached)"
        if current_branch != self.branch:
            return self._set(
                "blocked",
                f"Self update only runs on {self.branch}; current branch is {current_branch}",
                branch=current_branch,
            )

        try:
            self._git("fetch", self.remote, self.branch, "--prune", timeout=300)
            local_sha = self._git("rev-parse", "HEAD")
            remote_sha = self._git("rev-parse", f"{self.remote}/{self.branch}")
        except Exception as exc:
            return self._set("error", f"Self-update fetch failed: {exc}")

        if local_sha == remote_sha:
            return self._set(
                "current",
                "Orchestrator is up to date",
                local_sha=local_sha,
                remote_sha=remote_sha,
                branch=current_branch,
            )

        ancestor = self._run(
            ["git", "merge-base", "--is-ancestor", "HEAD", f"{self.remote}/{self.branch}"],
            check=False,
        ).returncode == 0
        if not ancestor:
            return self._set(
                "blocked",
                "Local orchestrator branch diverged from origin/main; automatic update refused",
                local_sha=local_sha,
                remote_sha=remote_sha,
                branch=current_branch,
            )

        if active_task:
            return self._set(
                "pending",
                "Update available; waiting for active managed-project task to finish",
                local_sha=local_sha,
                remote_sha=remote_sha,
                branch=current_branch,
            )

        status_raw = self._run(
            ["git", "status", "--porcelain", "--untracked-files=no"]
        ).stdout
        status_lines = [line for line in status_raw.splitlines() if line.strip()]
        registry_rel = self._registry_rel()
        changed_paths = [line[3:].strip().replace("\\", "/") for line in status_lines if len(line) >= 4]
        disallowed = [path for path in changed_paths if not registry_rel or path != registry_rel]
        if disallowed:
            return self._set(
                "blocked",
                "Update available but orchestrator source has local code changes: " + ", ".join(disallowed),
                local_sha=local_sha,
                remote_sha=remote_sha,
                branch=current_branch,
            )

        registry_backup: str | None = None
        registry_dirty = bool(registry_rel and registry_rel in changed_paths)
        try:
            if registry_dirty:
                registry_backup = self.registry_file.read_text(encoding="utf-8")
                # Validate before touching the working tree.
                json.loads(registry_backup)
                self._git("checkout", "--", registry_rel)

            self._git("merge", "--ff-only", f"{self.remote}/{self.branch}", timeout=300)

            if registry_backup is not None and registry_rel:
                remote_registry = self.registry_file.read_text(encoding="utf-8")
                merged = self._merge_registry(remote_registry, registry_backup)
                self.registry_file.write_text(merged, encoding="utf-8")

            new_sha = self._git("rev-parse", "HEAD")
            return self._set(
                "updated",
                "Orchestrator updated successfully; restart requested",
                local_sha=new_sha,
                previous_sha=local_sha,
                remote_sha=remote_sha,
                branch=current_branch,
            )
        except Exception as exc:
            if registry_backup is not None and registry_rel:
                try:
                    self.registry_file.write_text(registry_backup, encoding="utf-8")
                except OSError:
                    pass
            return self._set(
                "error",
                f"Self update failed: {exc}",
                local_sha=local_sha,
                remote_sha=remote_sha,
                branch=current_branch,
            )
