# Orchestrator Continuous Improvement & Self-Healing Guidelines

## Automation & Self-Healing Protocol
1. **Task Completion**:
   - Every completed task or bugfix must run unit tests: `python -m unittest discover tests`.
   - Automatically commit and push all changes to `origin/main`.
   - Ensure the Orchestrator server (`python -u app.py`) is running in the background.

2. **Per-Task Logging**:
   - Detailed task execution logs must be stored per task in `.orchestrator-runtime/task-logs/issue_<number>.log`.
   - Local system errors or executor failures must remain local in task logs and not spam GitHub issues.

3. **Continuous Monitoring & Healing**:
   - The system checks every 10 minutes for errors: server health, task leases, BLOCKED states, model configuration issues, and cycle/condition issues.
   - When errors are found, investigate code and logs directly (no HTML visual inspection unless layout is broken).
   - Fix issues thoroughly, re-test, restart server daemon, and push to git to maintain continuous self-improvement.

4. **Mandatory Post-Merge GPT Audit Rule**:
   - Regardless of which reviewer approved the PR (ChatGPT, Gemini on single-thread AGY, or human), after any MR/PR is merged into the base branch, ChatGPT must always be triggered to perform a comprehensive post-merge audit across all changed files and integration points.
   - If defects, regressions, or follow-up improvements are identified, ChatGPT must submit a fix request: either by requesting rework directly in the canonical task issue or by creating a new canonical Issue (e.g. `[issueM] Fix post-merge issues from [issueN]: <description>`) with prerequisite `condition: ["issueN"]` so Orchestrator and AGY can immediately execute the fixes.

