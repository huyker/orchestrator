from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Registry:
    def __init__(self, path: Path):
        self.path = path
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1 or not isinstance(raw.get("projects"), list):
            raise ValueError("Invalid projects.json")
        projects: dict[str, dict[str, Any]] = {}
        for item in raw["projects"]:
            if not item.get("enabled", True):
                continue
            project_id = str(item.get("id", "")).strip()
            repo = str(item.get("repo", "")).strip()
            issues_repo = str(item.get("issues_repo") or repo).strip()
            if not project_id or "/" not in repo or "/" not in issues_repo:
                raise ValueError(f"Invalid project registry entry: {item}")
            if project_id in projects:
                raise ValueError(f"Duplicate project id: {project_id}")
            projects[project_id] = {**item, "issues_repo": issues_repo}
        self.projects = projects

    def resolve(self, project_id: str) -> dict[str, Any]:
        item = self.projects.get(project_id)
        if not item:
            raise ValueError(f"Unknown/disabled project: {project_id}")
        return item

    def list(self) -> list[dict[str, Any]]:
        return [self.projects[key] for key in sorted(self.projects)]


def safe_path(root: Path, rel: str) -> Path:
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise ValueError(f"Absolute project path is not allowed: {rel}")
    candidate = (root / rel_path).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Project path escapes repository root: {rel}") from exc
    return candidate


def load_catalog(root: Path, manifest_path: str) -> dict[str, Any]:
    manifest_file = safe_path(root, manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if int(manifest.get("schema_version", 0)) not in (1, 2, 3):
        raise ValueError("Unsupported project manifest schema_version")
    if not manifest.get("project") or not manifest.get("repository"):
        raise ValueError("Project manifest must declare project and repository")

    agents_dir = safe_path(root, manifest.get("agent_profiles_dir", ".orchestrator/agents"))
    tasks_dir = safe_path(root, manifest.get("task_profiles_dir", ".orchestrator/tasks"))
    agents: dict[str, dict] = {}
    tasks: dict[str, dict] = {}

    if agents_dir.is_dir():
        for file in sorted(agents_dir.glob("*.json")):
            row = json.loads(file.read_text(encoding="utf-8"))
            key = row.get("id", file.stem)
            if key in agents:
                raise ValueError(f"Duplicate agent profile id: {key}")
            agents[key] = row

    if tasks_dir.is_dir():
        for file in sorted(tasks_dir.glob("*.json")):
            row = json.loads(file.read_text(encoding="utf-8"))
            key = row.get("id", file.stem)
            if key in tasks:
                raise ValueError(f"Duplicate task profile id: {key}")
            tasks[key] = row

    plans: list[str] = []
    for pattern in manifest.get("plan_globs", []):
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError(f"Unsafe plan glob: {pattern}")
        plans.extend(path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file())

    return {
        "manifest": manifest,
        "agents": agents,
        "tasks": tasks,
        "plans": sorted(set(plans)),
    }


def resolve_profiles(task: dict, catalog: dict) -> tuple[dict | None, dict, dict]:
    profiles = catalog["tasks"]
    profile = None
    explicit = task.get("task_profile")
    if explicit:
        profile = profiles.get(explicit)
        if not profile:
            raise ValueError(f"Unknown task_profile: {explicit}")
    else:
        matches = [row for row in profiles.values() if task["type"] in row.get("task_types", [])]
        if len(matches) == 1:
            profile = matches[0]
        elif len(matches) > 1:
            raise ValueError("Multiple task profiles match; set task_profile in Issue")
        else:
            raise ValueError(f"No task profile matches type: {task['type']}")

    executor_id = task.get("executor_profile") or profile.get("executor_profile")
    reviewer_id = task.get("reviewer_profile") or profile.get("reviewer_profile")
    executor = catalog["agents"].get(executor_id)
    reviewer = catalog["agents"].get(reviewer_id)
    if not executor or executor.get("role") != "executor":
        raise ValueError(f"Invalid executor profile: {executor_id}")
    if not reviewer or reviewer.get("role") != "reviewer":
        raise ValueError(f"Invalid reviewer profile: {reviewer_id}")
    return profile, executor, reviewer
