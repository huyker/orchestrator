# GitHub Issue Protocol v1

> Detailed ChatGPT ↔ Orchestrator ↔ AGY messaging, event naming, sync/reconciliation, `/review`, and rework protocol: [`CHATGPT_AGY_COMMUNICATION_PROTOCOL.md`](./CHATGPT_AGY_COMMUNICATION_PROTOCOL.md).

For **managed projects**, GitHub Issue is the task/message source of truth. Each managed project owns its Issue queue via `projects.json -> issues_repo`; project repositories store plans/rules/agent profiles/source code, but **not active task transport/state files in source branches**.

This protocol does not govern development of `huyker/orchestrator` itself. Orchestrator code changes are implemented/reviewed/merged directly without creating runtime task Issues.

## Bootstrap prerequisite

The local dashboard must first start successfully and pass both `/api/health` and `/api/status`. Before `dashboard_verified` is persisted, the scheduler cannot poll/claim/process managed-project Issues.

## Task contract

After dashboard verification, create/open one Issue in the selected project's configured `issues_repo` (defaults to the project repo) with lifecycle label `orch:ready` and exactly one block:

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
  "title": "Example task",
  "objective": "Implement the behavior",
  "task_profile": "gameplay-code",
  "plan_refs": ["docs/plans/example.md"],
  "instructions": [],
  "acceptance": ["criterion"],
  "prohibited": ["do not change unrelated assets"],
  "checks": {
    "require_substantive_diff": true,
    "required_paths": [],
    "required_diff_globs": [],
    "forbidden_diff_globs": [],
    "test_profiles": ["gameplay_v1"]
  },
  "review": {"independent": true, "min_score": 100, "max_rework": 5}
}
```

Changing the contract requires incrementing `revision`. Same-revision body mutation is fail-closed.

## Commands: GPT/User -> local

Commands are Issue comments from an allowed GitHub username. Each command comment is consumed once.

Answer the exact pending question:

```orchestrator-command
{"command":"answer","revision":1,"question_id":"targeting-choice","answer":"Use nearest target"}
```

When a user gate is reached, local commits/pushes only the gate-stage artifacts to the same task branch/PR and posts the PR URL, artifact digest and exact PR head SHA to the Issue. The user reviews that GitHub-visible concept before detailed production.

Approve the exact gate artifact + PR head:

```orchestrator-command
{"command":"approve_gate","revision":1,"gate_id":"asset-concept","artifact_digest":"sha256...","pr_head_sha":"<exact concept head sha>"}
```

If the PR head changes before approval, the pending approval is invalidated and the gate is regenerated.

External GPT review is bound to an exact review cycle and PR head SHA:

```orchestrator-command
{"command":"external_review","revision":1,"review_cycle":2,"pr_head_sha":"<sha>","verdict":"PASS","findings":[]}
```

or:

```orchestrator-command
{"command":"external_review","revision":1,"review_cycle":2,"pr_head_sha":"<sha>","verdict":"FIX_REQUIRED","findings":["..."]}
```

Retry a blocked task:

```orchestrator-command
{"command":"retry","revision":1}
```

The dashboard Retry button posts this same command to the Issue; it does not bypass GitHub.

## Events: local -> GPT/User

Local writes structured `orchestrator-event` comments, including:

- `started`
- `question`
- `answer_received`
- `user_gate_required`
- `user_gate_approved`
- `acceptance_failed`
- `review_failed`
- `ready_for_gpt_review`
- `gpt_fix_required`
- `gpt_approved`
- `recovered_after_restart`
- `blocked`
- `complete`

## Lifecycle

`READY -> RUNNING -> [QUESTION/GATE/REWORK]* -> GPT_REVIEW -> APPROVED -> MERGED -> DONE`

Only one task lease exists globally per local orchestrator state database. Lease acquisition is atomic and stale leases are recovered after restart.
