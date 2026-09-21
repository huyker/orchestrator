# GitHub Issue Protocol

## Task transport

The Issue body is the immutable task/revision contract. Target repositories must not store active task payload/status files.

Use exactly one block:

```orchestrator-task
{
  "schema_version": 1,
  "revision": 1,
  "task_id": "GAME-0001",
  "project": "gamegit",
  "target_repo": "huyker/game",
  "base_branch": "main",
  "type": "code",
  "priority": 0,
  "title": "Example",
  "objective": "Implement the requested behavior",
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
    "test_profiles": []
  },
  "review": {"independent": true, "min_score": 100}
}
```

## GPT/User -> local commands

All commands are Issue comments from an allowed GitHub username.

Answer a local question:

```orchestrator-command
{"command":"answer","revision":1,"question_id":"id","answer":"..."}
```

Retry same task after deterministic/local failure:

```orchestrator-command
{"command":"retry","revision":1}
```

External GPT review:

```orchestrator-command
{"command":"external_review","revision":1,"verdict":"PASS","findings":[]}
```

or:

```orchestrator-command
{"command":"external_review","revision":1,"verdict":"FIX_REQUIRED","findings":["..."]}
```

## Local -> GPT/User

Local posts `orchestrator-event` comments for: started, question, acceptance_failed, ready_for_gpt_review, gpt_fix_required, complete, blocked.

Exactly one task is active globally. The next `orch:ready` Issue is not claimed until the active task is complete or explicitly released.
