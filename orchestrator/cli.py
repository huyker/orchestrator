from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

from .dashboard import serve_dashboard
from .engine import OrchestratorEngine
from .models import Settings


def load_env(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(prog="issue-orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Run worker loop + local dashboard")
    sub.add_parser("dashboard", help="Run dashboard only")
    sub.add_parser("worker", help="Run headless worker loop")
    sub.add_parser("once", help="Process/reconcile one scheduler tick")
    sub.add_parser("sync", help="Clone/fetch all registered projects and load catalogs")
    sub.add_parser("projects", help="List registered projects")
    sub.add_parser("status", help="Print local runtime snapshot as JSON")
    sub.add_parser("bootstrap-labels", help="Create required orch:* labels in control repo")
    graph = sub.add_parser("graphify-update", help="Build/update Graphify graph for one project")
    graph.add_argument("project")
    args = parser.parse_args()

    settings = Settings.from_env()
    engine = OrchestratorEngine(settings)

    if args.command == "serve":
        engine.github.ensure_labels(settings.control_repo)
        serve_dashboard(engine, settings.dashboard_host, settings.dashboard_port, with_engine_loop=True)
    elif args.command == "dashboard":
        serve_dashboard(engine, settings.dashboard_host, settings.dashboard_port, with_engine_loop=False)
    elif args.command == "worker":
        engine.github.ensure_labels(settings.control_repo)
        engine.serve_loop(threading.Event())
    elif args.command == "once":
        engine.tick()
    elif args.command == "sync":
        print(json.dumps(engine.sync_projects(), ensure_ascii=False, indent=2))
    elif args.command == "projects":
        for project in engine.registry.list():
            print(f"{project['id']}: {project['repo']} ({project.get('default_branch', 'main')})")
    elif args.command == "status":
        print(json.dumps(engine.snapshot(), ensure_ascii=False, indent=2))
    elif args.command == "bootstrap-labels":
        engine.github.ensure_labels(settings.control_repo)
        print("orchestrator labels ready")
    elif args.command == "graphify-update":
        print(json.dumps(engine.update_graphify(args.project), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
