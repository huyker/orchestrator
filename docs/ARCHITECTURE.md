# Architecture

## Separation of responsibilities

### 1. GPT / User

- creates/revises the canonical GitHub Issue task contract;
- answers local-agent questions through Issue comments;
- approves explicit user gates through Issue comments;
- performs external review bound to an exact PR head SHA/review cycle.

### 2. `huyker/orchestrator`

Generic local control plane. It owns no GameGit product logic.

Components:

- **GitHub client** — Issue queue, comments/events, labels, PR metadata;
- **State store** — atomic single-task lease, heartbeat, command de-duplication, event history;
- **Project registry** — maps a project id to a GitHub repository + manifest;
- **Project catalog loader** — loads project-owned plans, task profiles and agent profiles;
- **Workspace manager** — clone/fetch/worktree/branch/PR handoff;
- **Graphify adapter** — optional codebase intelligence/context provider;
- **Execution engine** — task lifecycle, deterministic acceptance, reviewer/rework loop;
- **Dashboard** — local observability and safe operational controls.

### 3. Managed project repository (`huyker/game`)

Owns:

- product/game rules;
- plans/specifications;
- project source/assets;
- project-specific agent profiles;
- project-specific task profiles;
- named test profiles;
- protected paths and user-gate policy;
- optional Graphify config.

It does not own active task queue/state files.

## Data flow

```text
GPT/User
   │
   ▼
GitHub Issue in orchestrator repo
(task contract + Q&A + events + review decision)
   │
   ▼
Local Orchestrator
   ├── atomic lease / recovery
   ├── project registry
   ├── clone/sync target repo
   ├── load .orchestrator/project.json
   ├── load agent/task profiles
   ├── Graphify query (advisory)
   └── local AGY executor/reviewer
            │
            ▼
       target task branch / PR
            │
            ▼
      GPT external review
            │
       Issue command PASS/FIX
            │
            ▼
       merge → Issue complete
```

## Source-of-truth hierarchy

1. **Issue task contract** — what to do now.
2. **Project rules/plans** — project context and constraints.
3. **Actual target branch/PR diff** — implementation evidence.
4. **Acceptance/test/reviewer evidence** — validation.
5. **Graphify** — advisory retrieval/impact intelligence only.

Graphify can never activate work or override an Issue contract.

## Failure model

- duplicate local processes → atomic lease prevents second claim;
- process crash while agent runs → heartbeat expires; next instance takes stale lease and re-enters same task/worktree;
- old Issue command → consumed-comment table + event floor prevents replay;
- wrong answer → `question_id` mismatch ignored;
- stale GPT review → PR head SHA/review cycle mismatch ignored;
- reviewer FAIL → same Issue/branch rework, not a new task;
- local repo cache loss → remote task branch is detected and resumed;
- protected control files changed → deterministic acceptance fails;
- required user gate missing → task fails closed;
- hanging named test → timeout terminates test and records evidence.
