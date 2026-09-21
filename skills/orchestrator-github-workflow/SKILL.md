---
name: orchestrator-github-workflow
description: Manage ChatGPT ↔ local Orchestrator ↔ AGY work through GitHub Issues and PRs. Use when the user asks to create/hand off a managed-project task, answer an AGY question, inspect task status, run /review, review AGY work, request fixes, approve a user gate, retry a blocked task, or reconcile open orchestrator Issues. Always use the canonical Issue thread and the current GitHub state.
---

# Orchestrator GitHub Workflow Skill

This skill governs communication between ChatGPT, the user's local Orchestrator, and AGY.

The default control repository is `huyker/orchestrator`. Managed projects are discovered from its current `projects.json`; never hard-code the active managed-project list.

## First rule: GitHub is the task source of truth

For every invocation:

1. Read the current `huyker/orchestrator/projects.json`.
2. Read the current `docs/CHATGPT_AGY_COMMUNICATION_PROTOCOL.md` when the requested action involves task creation, Q&A, review, rework, or lifecycle mutation.
3. Use the GitHub connector/API for Issues, comments, PRs, branches, commits and diffs.
4. Do not rely on stale chat context when current GitHub state can be read.
5. Do not use generic web search to operate on these repositories when the GitHub connector is available.

If GitHub access is unavailable, say that current GitHub state cannot be verified. Do not invent task state, review results, PR numbers, comments, labels, or SHAs.

## Canonical task rule

One task = one canonical GitHub Issue.

Issue title:

```text
[issue<ID>] <short title>
```

All status, Q&A, rework and review messages for that task stay as comments in that same Issue.

Never create a new Issue merely to represent:

- AGY started;
- AGY finished;
- a question;
- an answer;
- GPT requested fixes;
- GPT approved;
- retry;
- recovery;
- merge/done.

Create a new Issue only for genuinely new/out-of-scope work.

## Managed-project discovery

Read `projects.json` from `huyker/orchestrator`.

For every enabled project, use:

- `id` as project id;
- `repo` as code repository;
- `issues_repo` as task/message repository;
- `default_branch` as default target branch;
- `manifest_path` to locate project-owned orchestration config.

When the user names a project explicitly, match it against registry id/repo.

When the user gives no project and asks `/review`, scan every enabled managed project's `issues_repo`.

## Creating a task

When the user asks ChatGPT to hand work to AGY/local Orchestrator:

1. Read the target project's current rules/plans/config needed to write a concrete task.
2. Search open and closed Issues in the target `issues_repo` for titles matching `[issueN]`.
3. Allocate the next logical id as `max(N)+1`. GitHub Issue number is separate from logical issue id.
4. Create exactly one canonical Issue with:
   - title `[issueN] <title>`;
   - one `orchestrator-task` JSON block;
   - lifecycle label `orch:ready`.
5. Set `review.reviewer` explicitly. Use `GPT` when ChatGPT is intended to perform final external review.
6. Do not create implementation branches or PRs on ChatGPT's behalf unless the user specifically asks; local Orchestrator/AGY owns task execution.

A task contract must include at least:

```json
{
  "schema_version": 1,
  "revision": 1,
  "task_id": "PROJECT-...",
  "project": "<registry id>",
  "target_repo": "<owner/repo>",
  "base_branch": "<branch>",
  "type": "<task type>",
  "priority": 0,
  "title": "...",
  "objective": "...",
  "task_profile": "...",
  "plan_refs": [],
  "instructions": [],
  "acceptance": [],
  "prohibited": [],
  "checks": {
    "require_substantive_diff": true,
    "required_paths": [],
    "required_diff_globs": [],
    "forbidden_diff_globs": [],
    "test_profiles": []
  },
  "review": {
    "reviewer": "GPT",
    "independent": true,
    "min_score": 100,
    "max_rework": 5
  }
}
```

Do not mutate the task body without incrementing `revision`.

## Event naming

Human-readable event headers use:

```text
[issue<ID>_<event>_by<actor>]
```

Common events:

```text
[issue1_started_byAGY]
[issue1_question_byAGY]
[issue1_answer_byGPT]
[issue1_update_byGPT]
[issue1_gate_required_byAGY]
[issue1_gate_approved_byGPT]
[issue1_fixdone_byAGY]
[issue1_review_fix_byGPT]
[issue1_review_pass_byGPT]
[issue1_blocked_byAGY]
[issue1_recovered_byORCH]
[issue1_retry_byGPT]
[issue1_done_byORCH]
```

Prefer a structured `orchestrator-event` JSON block in important comments. Bind events to exact revision and, for review/gates, exact PR HEAD SHA.

## Answering AGY questions

When the user asks ChatGPT to answer a pending AGY question:

1. Fetch the canonical Issue and comments.
2. Identify the latest unresolved `[issueX_question_byAGY]`.
3. Preserve exact `question_id` and current task `revision`.
4. Post the answer in the same Issue as `[issueX_answer_byGPT]`.
5. Do not create a new Issue.

If more than one unresolved question exists, distinguish them by `question_id`; never bind an answer by guess.

## /review behavior

The literal command `/review` means: review the complete current GPT review queue across all managed projects.

Do not interpret it as "review only the current chat" or "review the last task mentioned."

### Review discovery

For each enabled project in `projects.json`:

1. Find open Issues in `issues_repo` with lifecycle label `orch:gpt-review`.
2. Parse the current `orchestrator-task`.
3. Keep only tasks where `review.reviewer` is `GPT` or contains `GPT`.
4. Review every matching Issue.

If the user specifies a project or Issue, narrow the queue accordingly. Otherwise scan all managed projects.

### Required review evidence

For each review candidate fetch and verify:

1. canonical Issue body and current revision;
2. relevant Issue comments, especially latest `fixdone_byAGY`;
3. linked PR;
4. current PR HEAD SHA;
5. PR diff/current changed files;
6. test/acceptance evidence;
7. AGY/local independent review evidence;
8. project rules/plans relevant to acceptance;
9. CI/check status when applicable.

Do not PASS because AGY claims "PASS", "100/100", or all tests passed. Inspect the actual implementation/diff/artifact against the Issue contract.

### Review result: FIX_REQUIRED

If any in-scope acceptance criterion fails or evidence is insufficient:

1. post a comment in the same Issue:
   `[issueX_review_fix_byGPT]`;
2. include:
   - `revision`;
   - `review_cycle`;
   - PR number;
   - exact current `head_sha`;
   - concrete findings;
   - expected vs actual behavior;
   - required fix/evidence;
3. move lifecycle to `orch:rework` if the ChatGPT GitHub connection is permitted to mutate labels;
4. rework must use the same Issue, same task branch and same PR.

Do not create a new Issue for an in-scope defect.

### Review result: PASS

Only PASS when all acceptance criteria are verified against the current PR HEAD.

Post in the same Issue:

```text
[issueX_review_pass_byGPT]

revision: <revision>
review_cycle: <cycle>
pr: <number>
head_sha: <exact current head>

Verdict: PASS
...
```

Then move lifecycle to `orch:approved` when permitted.

A PASS applies only to the exact tuple:

```text
issue_id + revision + review_cycle + PR + head_sha
```

If the PR HEAD changes afterward, the old PASS is stale and cannot authorize merge.

## Review output in ChatGPT

After operating on GitHub, summarize each reviewed task compactly:

```text
Project: gamegit
Issue #42 — [issue7] ...
PR #17
HEAD: abc123
Result: FIX_REQUIRED

- AC #1: PASS
- AC #2: FAIL — ...
- Tests: ...
- Action: findings posted to the same Issue
```

For multiple tasks, report every task reviewed. Do not silently skip a matching `orch:gpt-review` Issue.

## Rework rule

The rework loop is always:

```text
same Issue
→ same task branch
→ same PR
→ new HEAD SHA
→ new fixdone_byAGY
→ orch:gpt-review
→ /review again
```

Only create a follow-up Issue when the finding is genuinely outside the current task scope. When doing so, link the follow-up from the canonical Issue.

## User gates

For an explicit user/concept gate:

1. verify the exact pending gate id;
2. verify artifact digest if present;
3. verify the exact PR HEAD SHA;
4. post `[issueX_gate_approved_byGPT]` only when the user's approval is explicit;
5. never treat a generic positive comment as approval for a different artifact/HEAD.

## Retry and blocked work

A retry request stays in the same Issue:

```text
[issueX_retry_byGPT]
```

Include current revision and reason.

Do not bypass GitHub state by telling local agents to retry out-of-band.

## Sync/reconciliation principles

When checking status or recovering context:

- scan current open managed Issues from GitHub;
- treat local Orchestrator DB/dashboard state as runtime cache, not canonical task truth;
- inspect current labels, revision, comments, PR state and HEAD;
- ignore duplicate/replayed structured events by `event_id`;
- fail closed on same-revision task-body mutation;
- closed Issues are not executable;
- merged PR + open canonical Issue should reconcile toward DONE.

## Safety against stale context

Before any state-changing action, re-fetch the relevant Issue/PR if earlier data may have changed.

Never post a GPT review against a HEAD SHA that was inferred from memory or an old chat.

## Detailed protocol

The authoritative detailed communication specification lives in the current `huyker/orchestrator` repository:

```text
docs/CHATGPT_AGY_COMMUNICATION_PROTOCOL.md
```

Fetch the latest version when exact event schema, lifecycle behavior, sync semantics or AGY implementation details matter.
