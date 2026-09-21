from __future__ import annotations

import argparse

from .core import IssueOrchestrator, Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="issue-orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("once")
    sub.add_parser("projects")
    args = parser.parse_args()

    settings = Settings.from_env()
    orch = IssueOrchestrator(settings)
    if args.command == "serve":
        orch.serve()
    elif args.command == "once":
        orch.tick()
    else:
        for p in orch.registry.list():
            print(f"{p['id']}: {p['repo']} ({p.get('default_branch', 'main')})")


if __name__ == "__main__":
    main()
