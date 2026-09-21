# Architecture

## Separation of responsibilities

### 1. GPT / User

For managed projects only:
- creates/revises the canonical project GitHub Issue task contract;
- answers local-agent questions through project Issue comments;
- approves explicit user gates through project Issue comments;
- performs external review bound to an exact PR head SHA/review cycle.

For the orchestrator repository itself, GPT/user does not use the runtime Issue queue: orchestrator changes are implemented directly on a code branch, reviewed, and merged.

### 2. `huyker/orchestrator`

Generic local control plane. It owns no GameGit product logic.

Components:

- **GitHub client** — aggregates per-project Issue queues, comments/events, labels and PR metadata;
- **State store** — atomic single-task lease, heartbeat, command de-duplication, event history;
- **Project registry** — maps a project id to its code repository, Issue repository and manifest;
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

## Bootstrap gate

The managed-project task plane is disabled until the localhost dashboard has successfully bound and passed both `/api/health` and `/api/status` smoke checks. The verified state is persisted locally. The Issue scheduler, task claim path and retry operations fail closed before this gate.

## Data flow

```text
GPT/User
   │
   ▼
GitHub Issue in managed project's `issues_repo`
(e.g. private `huyker/game` for GameGit;
task contract + Q&A + events + review decision)
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


## Issue ownership

The orchestrator repository is **not** a managed-project task queue. Orchestrator development uses normal code branch/review/merge directly.

The orchestrator does not centralize managed-project tasks in its own public repository.

Each registry entry declares `issues_repo` (defaulting to `repo`). The scheduler aggregates `orch:ready` Issues across those repositories and verifies that the Issue task's `project` matches the registry entry for that Issue source.

This keeps private-project requirements, Q&A and review decisions inside the private project repository while preserving one local sequential scheduler across all projects.
