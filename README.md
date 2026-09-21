# Orchestrator

Project-independent local task orchestrator using **GitHub Issues as the message bus**.

- GPT creates/updates task Issues.
- Local orchestrator polls Issues and executes **one task at a time**.
- Target project configuration is loaded dynamically from the target repo.
- Questions, blockers, progress, review handoff and completion are posted back to the Issue.
- Code/assets live only in the target project branch/PR; task transport never lives in target source files.

Current registered project: `gamegit -> huyker/game`.

See `docs/PROTOCOL.md` for the Issue contract and structured comment protocol.
