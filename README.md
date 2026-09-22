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

- Python 3.11+ (Python 3.8+ supported)
- Git
- SSH access to managed GitHub repositories
- GitHub API access for Issue/PR operations (either `GITHUB_TOKEN` or GitHub CLI `gh`)
- local `agy` executable for Antigravity agent execution

Clone and configure:

```bash
git clone git@github.com:huyker/orchestrator.git
cd orchestrator
cp .env.example .env
```

### GitHub Authentication & Permissions

Git clone/fetch/pull/push use the machine's existing SSH credentials:
```bash
ssh -T git@github.com
```

For GitHub Issue & PR sync, choose either of the following methods:

**Method 1: Personal Access Token (Recommended & Quickest)**
1. Go to [GitHub Settings → Developer Settings → Personal access tokens](https://github.com/settings/tokens).
2. Generate a token (Classic) with the **`repo`** scope (Full control of private repositories).
3. Add to your `.env` file:
   ```env
   GITHUB_TOKEN=ghp_your_personal_access_token_here
   ```
   *(Or paste it directly into the prompt banner on the Orchestrator dashboard at `http://127.0.0.1:8766`).*

**Method 2: GitHub CLI (`gh`) with Device Code**
1. Install `gh` if not already installed (e.g. `conda install -c conda-forge gh` or `sudo apt install gh`).
2. Run interactive or device code authentication:
   ```bash
   gh auth login
   ```
   Select: **GitHub.com** → **HTTPS or SSH** → **Paste an authentication token** or **Login with a web browser (Device Code)**.
3. Verify status:
   ```bash
   gh auth status
   ```

Then run:

```bash
python app.py
```

No package installation is required for the orchestrator itself; it uses the Python standard library.

## Dashboard

The dashboard UI is based on the earlier Gemini/AGY control-plane dashboard from the GameFi GitLab workflow, adapted to the new GitHub-Issue architecture.

It includes:

- dark glass / cyan-purple control-plane visual language;
- overview metric cards;
- live runtime telemetry and event stream;
- managed-project list and project context panel;
- task cards rendered from GitHub Issues;
- six-stage lifecycle visualization:
  `Ready → Implement → Validate → QA → GPT Review → Done`;
- progress %, rework, blocked, user-gate and external-review callouts;
- **Add Project** modal.

### Add another managed Git project

Configure one managed storage root, for example:

```env
ORCH_MANAGED_ROOT=E:\
```

Then click **Add Project** and paste only the GitHub repository link:

```text
https://github.com/huyker/game.git
```

SSH form and `owner/repo` shorthand are also accepted.

The local project path is derived automatically as:

```text
<ORCH_MANAGED_ROOT>/<owner>/<repo>
```

For example:

```text
E:\huyker\game
```

When the repository does not exist locally, orchestrator clones it with the machine's SSH credentials. When it already exists, orchestrator verifies that its `origin` matches the registered GitHub repo, then runs `fetch` and `pull --ff-only` before loading the project manifest, Graphify state, plans and agents.

There is no Add Folder workflow and no per-project local path field in the registry. Task execution still uses isolated Git worktrees under the orchestrator runtime directory.

The registry stores repository identity and project metadata; filesystem placement comes only from `ORCH_MANAGED_ROOT`.

## Reusable ChatGPT workflow skill

The ChatGPT ↔ Orchestrator ↔ AGY task/review workflow is packaged as a reusable skill:

```text
skills/orchestrator-github-workflow/SKILL.md
```

It defines cross-project behavior for:

- creating canonical `[issueN]` tasks;
- answering AGY questions in the same Issue;
- `/review` across every registered managed project;
- binding GPT review to exact revision / review cycle / PR HEAD SHA;
- same-Issue / same-branch / same-PR rework;
- user gates, retries and reconciliation.

The skill always reloads the current `projects.json` and the current communication protocol from GitHub rather than trusting stale chat context.

## Orchestrator self update

After the one-time version containing this feature is pulled, `python app.py` keeps the orchestrator itself current automatically.

Every `ORCH_SELF_UPDATE_INTERVAL` seconds (default 15), the app:

1. fetches `origin/main`;
2. compares the running checkout with the remote head;
3. waits if a managed-project task is active;
4. refuses to overwrite local orchestrator code changes or a diverged branch;
5. fast-forwards to `origin/main` when safe;
6. preserves locally registered entries in `projects.json`;
7. shuts down the dashboard cleanly;
8. starts the updated `python app.py` automatically on the same URL.

The dashboard header shows `Updater: CURRENT / PENDING / BLOCKED / ERROR`.

Configuration:

```env
ORCH_SELF_UPDATE=1
ORCH_SELF_UPDATE_INTERVAL=15
ORCH_SELF_UPDATE_REMOTE=origin
ORCH_SELF_UPDATE_BRANCH=main
```

The self updater uses normal local Git/SSH credentials. It does not require GitHub API authentication.

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

If `gh auth status` is not healthy, the dashboard and SSH Git sync remain usable, but Issue/PR reads and writes remain disabled until the machine's GitHub CLI authentication is fixed.

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
ORCH_MANAGED_ROOT=E:\
ORCH_PROJECT_REGISTRY=projects.json
ORCH_RUNTIME_DIR=.orchestrator-runtime

ORCH_POLL_INTERVAL=5
ORCH_SYNC_INTERVAL=15
ORCH_SELF_UPDATE=1
ORCH_SELF_UPDATE_INTERVAL=15
ORCH_SELF_UPDATE_REMOTE=origin
ORCH_SELF_UPDATE_BRANCH=main

ORCH_LEASE_TIMEOUT=90
ORCH_AGENT_TIMEOUT=1800
ORCH_TEST_TIMEOUT=600
ORCH_AGY_BIN=agy
ORCH_AGENT_EFFORT=medium
# ORCH_ALLOWED_AUTHORS=huyker

ORCH_GIT_TRANSPORT=ssh
ORCH_DASHBOARD_HOST=127.0.0.1
ORCH_DASHBOARD_PORT=8766
ORCH_OPEN_BROWSER=1
```

No `GITHUB_TOKEN`, project folder path, or per-project workspace root is required. Set `ORCH_OPEN_BROWSER=0` if you do not want the app to open a browser automatically.

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
