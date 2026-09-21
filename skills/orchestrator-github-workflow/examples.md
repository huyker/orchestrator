# Workflow Examples

## Example: create first task

User:

```text
Giao cho gamegit sửa Foundation movement
```

Expected behavior:

1. Read `huyker/orchestrator/projects.json`.
2. Resolve `gamegit -> huyker/game`.
3. Read relevant GameGit rules/plans.
4. Find max existing logical `[issueN]`.
5. Create `[issueN+1] Fix Foundation movement` in the project's `issues_repo`.
6. Add `orch:ready`.
7. Return the Issue link and task summary.

## Example: AGY question

Issue comment:

```text
[issue7_question_byAGY]

question_id: q-targeting
revision: 2

Should the hero target nearest or lowest-HP enemy?
```

GPT answer:

```text
[issue7_answer_byGPT]

question_id: q-targeting
revision: 2

Use nearest target.
```

Post the answer to the same Issue.

## Example: /review

The user sends:

```text
/review
```

The skill must:

1. read all enabled managed projects;
2. find all open `orch:gpt-review` Issues with GPT reviewer;
3. inspect each latest PR HEAD and actual diff;
4. post PASS or FIX_REQUIRED into each same Issue;
5. summarize all review results in chat.

## Example: rework

AGY posts:

```text
[issue7_fixdone_byAGY]

revision: 2
review_cycle: 1
pr: 19
head_sha: abc123
```

GPT finds one defect and posts:

```text
[issue7_review_fix_byGPT]

revision: 2
review_cycle: 1
pr: 19
head_sha: abc123

Finding:
Foundation link state is stale after movement.

Required fix:
Recompute link graph after Foundation movement and add regression coverage.
```

AGY reworks **Issue 7 / same branch / PR 19**, pushes a new HEAD, posts a new `fixdone_byAGY`, and the next `/review` reviews that new HEAD.

## Example: out-of-scope finding

During review, GPT notices an unrelated architecture weakness.

Do not put that unrelated implementation into the current task.

Create a follow-up Issue only if useful and add a link from the original Issue:

```text
[issue7_followup_created_byGPT]

followup_issue: #55
reason: Independent architecture debt discovered during review.
```
