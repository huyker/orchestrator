# Orchestrator

Local all-in-one control center for managed projects such as GameGit.

For managed projects, the runtime flow is:

**GPT / User → project GitHub Issue → local orchestrator → project agent → PR → GPT review → merge**

The orchestrator repository itself is developed directly through normal code review/merge. It is not a runtime task queue.

## Run

There is only one runtime entrypoint:

```bash
python app.py
```

That command starts everything:

1. loads `.env`;
2. starts the localhost dashboard;
3. verifies `/api/health` and `/api/status`;
4. opens the dashboard in the browser by default;
5. continuously syncs every registered project;
6. reloads project manifests, rules, plans, agent profiles and task profiles automatically;
7. updates Graphify automatically when an enabled project changes;
8. creates lifecycle labels in managed-project Issue repositories;
9. only after the dashboard and first project sync are healthy, starts the sequential Issue worker;
10. keeps syncing and polling continuously until the app stops.

You do not need to run separate sync, worker, dashboard, once or Graphify commands.

Default dashboard:

```text
http://127.0.0.1:8766
```

## First-time setup

Requirements:

- Python 3.11+
- Git
- SSH access to managed private repositories, or configure HTTPS transport
- local `agy` executable for Antigravity agent execution
- GitHub token with access to managed repositories

Clone and configure:

```bash
git clone git@github.com:huyker/orchestrator.git
cd orchestrator
cp .env.example .env
```

You can run immediately without putting a token in `.env`:

```bash
python app.py
```

GitHub authentication is resolved in this order:

1. `GITHUB_TOKEN`;
2. `GH_TOKEN`;
3. existing GitHub CLI login from `gh auth token`;
4. if none is available, the dashboard still opens and shows **Connect GitHub**, where you can paste a token once.

A token entered in the dashboard is validated against GitHub, stored only in local `.env`, and activates the Issue worker without restarting the app.

`ORCH_CONTROL_REPO` is no longer required. Managed-project task communication is routed from `projects.json -> issues_repo`.

`ORCH_ALLOWED_AUTHORS` is also optional for the normal setup; when omitted, the app infers repository owners from `projects.json` (for example `huyker/game -> huyker`).

Then run:

```bash
python app.py
```

No package installation is required for the orchestrator itself; it uses the Python standard library.

## Automatic sync

The app continuously fetches registered projects. Default interval:

```env
ORCH_SYNC_INTERVAL=15
```

When a project HEAD changes, the app reloads:

- `.orchestrator/project.json`
- project rules/context
- plans
- agent profiles
- task profiles
- Graphify state

The dashboard shows the last automatic sync time and current project state.

If the first project sync fails, the dashboard still stays available and shows the error. The managed-project Issue worker remains disabled and the app retries automatically.

## Dashboard bootstrap gate

Managed-project Issue handling is fail-closed.

Before the dashboard is verified, orchestrator will not:

- read the managed-project task queue;
- claim a task;
- mutate lifecycle labels;
- retry a task;
- execute a project agent.

The Issue worker starts only after:

```text
dashboard bind
    ↓
/api/health OK
    ↓
/api/status OK
    ↓
dashboard_verified
    ↓
GitHub authenticated
    ↓
first project sync OK
    ↓
Issue worker enabled
```

If GitHub is not authenticated, the dashboard remains usable and project Git sync keeps retrying, but Issue reads/writes remain disabled.

Successful startup prints:

```text
Orchestrator dashboard verified: http://127.0.0.1:8766
Auto sync interval: 15s
Managed-project Issue worker will start automatically after the first successful project sync.
```

## Dashboard controls

The dashboard is operational visibility, not a shell.

It shows:

- registered projects;
- automatic sync state;
- project agents/task profiles/plans;
- Graphify state;
- active task;
- managed-project Issue queue;
- recent runtime events.

Manual controls are intentionally small:

- Pause / Resume
- Retry active task via its GitHub Issue
- Refresh

Project sync and normal scheduling are automatic.

## Project registry

Projects are registered in `projects.json`.

Example:

```json
{
  "schema_version": 1,
  "projects": [
    {
      "id": "gamegit",
      "repo": "huyker/game",
      "issues_repo": "huyker/game",
      "default_branch": "main",
      "manifest_path": ".orchestrator/project.json",
      "enabled": true
    }
  ]
}
```

Adding another managed project should normally require only:

1. adding one registry entry;
2. adding that project's `.orchestrator/project.json`;
3. adding project-owned agent/task profiles.

Core orchestrator code should not need project-specific changes.

## Graphify

Graphify is optional codebase intelligence. GitHub Issue remains the task source of truth.

Install the official CLI separately on the local machine:

```bash
uv tool install graphifyy
# or
pipx install graphifyy
```

For Google Antigravity:

```bash
graphify antigravity install
```

When a project manifest enables Graphify, the all-in-one app:

- detects the CLI;
- builds the graph if missing;
- updates it automatically when the synced project HEAD changes;
- can inject bounded Graphify query context into executor/reviewer prompts.

Graphify never activates work and never overrides Issue requirements or project rules.

## Environment

Important settings:

```env
GITHUB_TOKEN=                           # optional at startup
# ORCH_CONTROL_REPO=huyker/orchestrator   # optional legacy fallback
ORCH_PROJECT_REGISTRY=projects.json
ORCH_RUNTIME_DIR=.orchestrator-runtime
ORCH_WORKSPACE_ROOT=.orchestrator-runtime/repos

ORCH_POLL_INTERVAL=5
ORCH_SYNC_INTERVAL=15
ORCH_LEASE_TIMEOUT=90
ORCH_AGENT_TIMEOUT=1800
ORCH_TEST_TIMEOUT=600

ORCH_AGY_BIN=agy
ORCH_AGENT_EFFORT=medium
# ORCH_ALLOWED_AUTHORS=huyker             # optional; inferred from registry by default

ORCH_GIT_TRANSPORT=ssh

ORCH_DASHBOARD_HOST=127.0.0.1
ORCH_DASHBOARD_PORT=8766
ORCH_OPEN_BROWSER=1
```

Set `ORCH_OPEN_BROWSER=0` if you do not want the app to open a browser automatically.

## Task ownership

For GameGit, task requirements, Q&A, status and GPT review live in `huyker/game` Issues.

The GameGit repository owns:

- product/game rules;
- plans/specifications;
- project-specific agent profiles;
- task profiles;
- source/assets;
- named test profiles;
- user-gate policy.

Orchestrator owns only the generic runtime and transport.

## Safety

- dashboard binds only to loopback;
- no arbitrary shell endpoint exists;
- only allowed GitHub authors can issue commands;
- one task lease is active globally;
- Issue commands are consumed once;
- answers bind to exact question ids;
- GPT review binds to exact PR head/review cycle;
- protected project paths fail closed;
- asset user gates fail closed;
- tests have timeouts;
- remote task branches can resume after local cache loss.

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```
