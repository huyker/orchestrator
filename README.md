# Issue Orchestrator

Local multi-project control center.

For **managed projects** such as GameGit, the runtime workflow is:

**GPT / User → project GitHub Issue → local orchestrator → project agent → PR → GPT review → merge**

The orchestrator repository itself is different: changes to `huyker/orchestrator` are implemented directly in code branches, reviewed, and merged. Do **not** create orchestrator runtime task Issues for orchestrator's own development.

`huyker/orchestrator` is generic. Each managed project owns its own Issue queue, rules, plans, agent/task profiles, source and PRs. For GameGit, both task Issues and implementation PRs live in private `huyker/game`.

## What this service owns

- GitHub Issue queue and lifecycle state
- one-task-at-a-time lease/locking
- restart recovery and command de-duplication
- project registry
- cloning/syncing target projects
- loading project-owned agent/task profiles
- isolated worktrees/branches and PR reuse
- local agent execution
- deterministic acceptance tests
- independent project reviewer handoff
- optional Graphify codebase intelligence
- localhost dashboard

It does **not** own GameGit product plans/rules/assets. Those stay in `huyker/game`.

---

## 1. Prerequisites

- Linux/macOS/WSL recommended
- Python 3.11+
- Git
- SSH access to every private target repo, or use HTTPS transport
- local `agy` command available for Antigravity agent execution
- GitHub token with access to the orchestrator admin repo and every managed project's configured Issue/code repo

### GitHub token permissions

For a fine-grained GitHub token, grant at least:

**`huyker/orchestrator`**
- Issues: Read & Write
- Contents: Read
- Metadata: Read

**target projects such as `huyker/game`**
- Issues: Read & Write
- Contents: Read & Write
- Pull requests: Read & Write
- Metadata: Read

The local Git process must also be able to push task branches. With the default SSH transport, verify:

```bash
ssh -T git@github.com
```

---

## 2. Install

```bash
git clone git@github.com:huyker/orchestrator.git
cd orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` and set at least:

```env
ORCH_CONTROL_REPO=huyker/orchestrator
GITHUB_TOKEN=github_pat_xxx
ORCH_ALLOWED_AUTHORS=huyker
```

The CLI automatically loads `.env` from the current directory.

---

## 3. Install Graphify (recommended)

Graphify is optional project intelligence; GitHub Issue remains the task source of truth.

Install the official package/CLI:

```bash
uv tool install graphifyy
# or: pipx install graphifyy
```

Register Graphify for Google Antigravity:

```bash
graphify antigravity install
```

Check:

```bash
graphify --help
```

For a project whose `.orchestrator/project.json` enables Graphify, orchestrator can build/update the graph before a task and query bounded graph context for executor/reviewer prompts.

Manual update:

```bash
issue-orchestrator graphify-update gamegit
```

Graphify remains advisory. It cannot replace the project Issue task contract, project rules or acceptance checks.

---

## 4. First run

Clone/fetch all registered projects and verify their manifests:

```bash
issue-orchestrator sync
```

List projects:

```bash
issue-orchestrator projects
```

Expected initially:

```text
gamegit: huyker/game (main)
```

**Do not create/hand off any managed-project task Issue yet.** Issue polling is hard-disabled until the dashboard bootstrap check succeeds.

---

## 5. Start the orchestrator + dashboard

Recommended command:

```bash
issue-orchestrator serve
```

Startup order is fail-closed:

1. bind the localhost dashboard;
2. probe `/api/health`;
3. probe `/api/status`;
4. persist `dashboard_verified`;
5. create/update lifecycle labels only in registered managed-project `issues_repo` repositories;
6. only then start the Issue worker.

Until step 4 succeeds, `tick()`, `worker`, `once`, retry, task claim and Issue processing are disabled.

Successful startup prints:

```text
Orchestrator dashboard verified: http://127.0.0.1:8766
```

Only **after that line appears** should GPT/user create or label managed-project tasks as `orch:ready`.

Open:

```text
http://127.0.0.1:8766
```

The dashboard shows:

- registered projects
- loaded agents and task profiles
- Graphify availability/graph state
- current active Issue/task
- Issue queue
- lifecycle state
- recent runtime events

Safe controls:

- **Start / Resume**
- **Pause**
- **Process once**
- **Sync projects**
- **Retry via Issue**
- **Update Graphify** per project

`Retry via Issue` posts a structured GitHub Issue command. The dashboard does not directly mutate task intent and has no arbitrary shell endpoint.

### Headless worker only

```bash
issue-orchestrator worker
```

This command refuses to start until a dashboard bootstrap has previously completed successfully against the same local runtime state.

### Dashboard only

```bash
issue-orchestrator dashboard
```

### One scheduler tick

```bash
issue-orchestrator once
```

### Local runtime snapshot

```bash
issue-orchestrator status
```

---

## 6. Project registry

`projects.json` is the orchestrator-level registry:

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

`issues_repo` is where GPT/local/reviewer task communication lives. It defaults to `repo`, so private projects keep their task contracts/Q&A private in the same repository. To add another project, register it here and add a compatible `.orchestrator/project.json` to that project repo. Core orchestrator code should not need project-specific changes.

---

## 7. Project manifest

Example fields in a target project:

```json
{
  "schema_version": 3,
  "project": "gamegit",
  "repository": "huyker/game",
  "default_branch": "main",
  "context_files": ["rule.md", "AGENTS.md"],
  "plan_globs": ["docs/plans/**/*.md"],
  "agent_profiles_dir": ".orchestrator/agents",
  "task_profiles_dir": ".orchestrator/tasks",
  "protected_paths": [".orchestrator/**", ".github/workflows/**"],
  "graphify": {
    "enabled": true,
    "required": false,
    "auto_update": "before_task",
    "query_context": true,
    "output_dir": "graphify-out"
  }
}
```

The target repo owns its own:

- project rules
- plans/specs
- agent configs
- task profiles
- test profiles
- product source/assets

---

## 8. Create a managed-project task

Prerequisite: dashboard status must show `dashboard_bootstrap.verified = true`.

Create the Issue in the selected project's configured `issues_repo` (for GameGit: `huyker/game`), add label `orch:ready`, and use exactly one task block:

```orchestrator-task
{
  "schema_version": 1,
  "revision": 1,
  "task_id": "GAME-0001",
  "project": "gamegit",
  "target_repo": "huyker/game",
  "base_branch": "main",
  "type": "gameplay",
  "priority": 0,
  "title": "Implement Foundation movement",
  "objective": "...",
  "task_profile": "gameplay-code",
  "plan_refs": ["docs/plans/foundation.md"],
  "acceptance": ["..."],
  "prohibited": ["..."],
  "checks": {
    "require_substantive_diff": true,
    "test_profiles": ["gameplay_v1"]
  },
  "review": {"independent": true, "min_score": 100, "max_rework": 5}
}
```

All questions, answers, progress and GPT review decisions remain on that Issue. See [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

---

## 9. Asset user gates

Asset task profiles may require explicit user approval.

The Issue includes a gate, for example:

```json
"user_gates": [
  {
    "id": "asset-concept",
    "required": true,
    "message": "Approve concept before detailed production"
  }
]
```

Local generates only gate-stage artifacts, computes their digest, commits/pushes those concept artifacts to the same task branch/PR, then posts `user_gate_required` with the PR URL and exact PR head SHA to the Issue. This keeps concept review 100% visible through GitHub.

Approval must match the exact gate id + digest + concept PR head:

```orchestrator-command
{"command":"approve_gate","revision":1,"gate_id":"asset-concept","artifact_digest":"<sha256>","pr_head_sha":"<exact concept head sha>"}
```

If the PR head changes, the pending gate is invalidated. No matching approval = no post-gate production.

---

## 10. Restart / recovery

Runtime state lives under:

```text
.orchestrator-runtime/
```

The SQLite lease is atomic. A second orchestrator process cannot claim a second task while a live lease exists.

If the process dies during `RUNNING`, heartbeat expiry allows a new process to take over the stale lease and re-enter the same Issue/worktree as `RECOVERING -> REWORK` rather than deadlocking the queue.

Remote task branches are detected and resumed if local runtime cache/worktrees were removed.

---

## 11. Troubleshooting

### Dashboard opens but project says `not synced`

```bash
issue-orchestrator sync
```

### Graphify says `CLI missing`

```bash
uv tool install graphifyy
graphify antigravity install
```

Then:

```bash
issue-orchestrator graphify-update gamegit
```

### `agy executable not found`

```bash
which agy
agy --help
```

Or set:

```env
ORCH_AGY_BIN=/absolute/path/to/agy
```

### Git push/auth failure

For SSH mode:

```bash
ssh -T git@github.com
```

For HTTPS mode:

```env
ORCH_GIT_TRANSPORT=https
```

The GitHub API token and Git clone/push credentials are separate concerns.

### Task is blocked

Read the latest `orchestrator-event` on the Issue. Fix the cause, then either post a valid `retry` command or use **Retry via Issue** in the dashboard.

---

## 12. Tests

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

CI runs the same suite on pull requests.

---

## Security model

- dashboard binds to loopback only
- allowed GitHub authors are explicit
- Issue commands are consumed once
- answers bind to exact `question_id`
- GPT review binds to exact PR head SHA + review cycle
- task project must match the registry
- project paths are constrained to the repository root
- project protected paths are fail-closed
- dashboard exposes no arbitrary command/shell API
- task test execution can only reference named commands from the trusted project manifest
