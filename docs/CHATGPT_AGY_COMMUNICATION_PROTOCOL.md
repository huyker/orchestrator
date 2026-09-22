# ChatGPT ↔ Orchestrator ↔ AGY GitHub Communication Protocol v1

> Mục tiêu: định nghĩa **một giao thức giao tiếp duy nhất qua GitHub Issues** để ChatGPT, local Orchestrator và AGY có thể nhắn tin, trả lời, đồng bộ trạng thái, bàn giao task, review và rework mà không mất context.
>
> Tài liệu này là **spec triển khai** cho AGY phát triển hệ thống giao tiếp tiếp theo.

---

## 1. Nguyên tắc cốt lõi

### 1.1. Một task = một canonical GitHub Issue

Mỗi công việc chỉ có **một Issue gốc**.

Ví dụ:

```text
[issue1] Fix Foundation movement and checkpoint behavior
```

Issue này là source of truth cho toàn bộ vòng đời:

```text
GPT/User tạo yêu cầu
→ Orchestrator nhận task
→ AGY xử lý
→ AGY hỏi
→ GPT/User trả lời
→ AGY báo hoàn thành
→ GPT review
→ AGY fix
→ GPT review lại
→ PASS
→ merge/done
```

**Không tạo Issue mới chỉ để báo trạng thái của Issue cũ.**

Ví dụ:

```text
[issue1_fixdone_byAGY] ...
```

là **comment/event trong Issue #1**, không phải một GitHub Issue mới.

---

## 2. Quy ước tên

### 2.1. Canonical Issue title

Format:

```text
[issue<ID>] <short title>
```

Ví dụ:

```text
[issue1] Fix Foundation movement
[issue2] Add Mage checkpoint gacha
[issue27] Rework Sanctuary movement VFX
```

Rules:

- `issue<ID>` là ID logic ổn định của task.
- Task contract bắt buộc có `"issue_id": "issue<ID>"` và giá trị này phải khớp title.
- Không đổi `issue<ID>` trong suốt vòng đời.
- Có thể sửa phần title phía sau nếu cần làm rõ nội dung.
- GitHub Issue number và logical issue ID là hai khái niệm khác nhau.

Ví dụ:

```text
GitHub Issue #42
Logical ID: issue7
Title: [issue7] Fix Barricade collision
```

---

## 3. Event header trong comment

Mọi message máy đọc được phải có **header ở dòng đầu tiên**.

Format tổng quát:

```text
[issue<ID>_<event>_by<actor>]
```

Ví dụ:

```text
[issue1_started_byAGY]
[issue1_question_byAGY]
[issue1_answer_byGPT]
[issue1_fixdone_byAGY]
[issue1_review_fix_byGPT]
[issue1_review_pass_byGPT]
```

Actor chuẩn:

- `GPT`
- `AGY`
- `USER`
- `ORCH`

Không dùng actor tuỳ ý nếu parser chưa đăng ký.

---

## 4. Danh sách event chuẩn

### 4.1. GPT/User → Orchestrator/AGY

#### Tạo task

Issue title:

```text
[issue1] <task title>
```

Issue body chứa `orchestrator-task` contract.

#### Trả lời câu hỏi

```text
[issue1_answer_byGPT]

question_id: targeting-rule
revision: 3

Use nearest target first.
```

#### Thay đổi/yêu cầu bổ sung trong cùng scope

```text
[issue1_update_byGPT]

revision: 4

Add requirement:
- Monsters behind Foundation must receive pursuit speed bonus.
```

Khi task contract thay đổi:

- tăng `revision`;
- cập nhật Issue body;
- comment event `issue_update_byGPT` để local nhận biết nhanh.

#### Yêu cầu fix sau review

```text
[issue1_review_fix_byGPT]

revision: 4
review_cycle: 2
pr: 17
head_sha: abc123

Findings:
1. Barricade linkage is not validated after Foundation moves.
2. Missing regression test for rear monster pursuit speed.
```

#### Review PASS

```text
[issue1_review_pass_byGPT]

revision: 4
review_cycle: 3
pr: 17
head_sha: def456

Verdict: PASS
All acceptance criteria verified.
```

#### Approve user gate

```text
[issue1_gate_approved_byGPT]

gate_id: asset-concept
revision: 4
head_sha: def456
artifact_digest: sha256:...
```

#### Merge bị chặn sau GPT PASS

```text
[issue1_merge_blocked_byGPT]

revision: 4
review_cycle: 3
pr: 17
head_sha: def456
reason_code: MERGE_CONFLICT

Details:
...
```

Event này chỉ dùng khi GPT đã PASS đúng HEAD nhưng GitHub chưa thể merge vì blocker kỹ thuật. Issue giữ mở và có thể tạm ở `orch:approved`. Nếu HEAD thay đổi thì PASS cũ mất hiệu lực và phải review lại.

#### Finalize sau merge

```text
[issue1_done_byGPT]

revision: 4
review_cycle: 3
pr: 17
reviewed_head_sha: def456
merge_sha: 999aaa
```

Đây là terminal success event chuẩn khi reviewer cuối cùng là GPT.

#### Retry

```text
[issue1_retry_byGPT]

revision: 4
reason: Retry after local runtime recovery.
```

---

### 4.2. AGY/Orchestrator → GPT/User

#### Task đã được nhận

```text
[issue1_started_byAGY]

revision: 4
branch: task/issue-42-game-0001
agent: 03_executor
```

#### Câu hỏi cần GPT/User trả lời

```text
[issue1_question_byAGY]

revision: 4
question_id: checkpoint-gacha-count

Question:
Should checkpoint gacha always offer 3 candidates or scale by checkpoint level?
```

#### Chờ user gate

```text
[issue1_gate_required_byAGY]

revision: 4
gate_id: asset-concept
pr: 17
head_sha: abc123
artifact_digest: sha256:...

Please review the concept before detailed production.
```

#### AGY hoàn thành implementation/fix

Đây chính là format tương ứng ví dụ:

```text
[issue1_fixdone_byAGY]

revision: 4
review_cycle: 2
pr: 17
head_sha: def456

Summary:
- Fixed Barricade linkage after Foundation movement.
- Added pursuit-speed regression test.

Evidence:
- tests: 43/43 PASS
- internal review: PASS
```

Ý nghĩa:

- AGY đã xử lý xong phần được giao;
- branch/PR đã được cập nhật;
- task **chưa được xem là DONE**;
- nếu reviewer là GPT, Orchestrator phải chuyển task sang `WAITING_GPT_REVIEW`.

#### AGY bị block

```text
[issue1_blocked_byAGY]

revision: 4
reason_code: TEST_INFRA_ERROR

Details:
...
```

#### Orchestrator recovery

```text
[issue1_recovered_byORCH]

revision: 4
branch: task/issue-42-game-0001
reason: stale lease recovered after restart
```

#### Task hoàn tất do reconciliation

Normal GPT-reviewed path dùng `[issue1_done_byGPT]`. `[issue1_done_byORCH]` chỉ dùng cho reconciliation/legacy/non-GPT-reviewer flow khi Orchestrator phát hiện PR đã được merge hợp lệ từ bên ngoài.

```text
[issue1_done_byORCH]

revision: 4
pr: 17
merge_sha: ...
```

---

## 5. Structured metadata block

Để parser ổn định, mọi event quan trọng nên có thêm block JSON.

Ví dụ:

```text
[issue1_fixdone_byAGY]

```orchestrator-event
{
  "schema_version": 1,
  "event_id": "issue1-fixdone-r4-c2-def456",
  "issue_id": "issue1",
  "event": "fixdone",
  "actor": "AGY",
  "revision": 4,
  "review_cycle": 2,
  "pr": 17,
  "head_sha": "def456",
  "timestamp": "2026-09-21T16:00:00Z"
}
```

Summary:
...
```

Rules:

- Header phục vụ người đọc nhanh.
- JSON block phục vụ machine parser.
- Parser ưu tiên JSON block.
- Nếu header và JSON conflict → fail closed.
- `event_id` phải unique và idempotent.

---

## 6. Task contract trong Issue body

Canonical Issue body phải có đúng một block:

```orchestrator-task
{
  "schema_version": 1,
  "revision": 4,
  "issue_id": "issue1",
  "condition": [],
  "task_id": "GAME-0001",
  "project": "gamegit",
  "target_repo": "huyker/game",
  "base_branch": "main",
  "type": "gameplay",
  "priority": 0,
  "title": "Fix Foundation movement",
  "objective": "...",
  "task_profile": "gameplay-code",
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


### 6.1. Dependency condition

Mỗi task contract bắt buộc có:

```json
"issue_id": "issue3",
"condition": ["issue1", "issue2"]
```

Ý nghĩa:

- `issue_id` là logical Issue ID, phải khớp với title `[issue3]`.
- `condition` là danh sách **logical Issue ID** cần hoàn thành trước.
- `condition: []` nghĩa là task độc lập, có thể chạy ngay khi `orch:ready` và còn worker slot.
- Không dùng GitHub Issue number trong `condition`.

Dependency được xem là **SATISFIED** chỉ khi Issue được tham chiếu đã hoàn thành thành công:

```text
valid canonical Issue
+ terminal success / valid done_byORCH
+ orch:done
+ Issue closed as completed
+ required PR merged (nếu task có PR)
= dependency satisfied
```

Các trạng thái sau **chưa đủ** để thỏa condition:

```text
orch:running
orch:question
orch:user-gate
orch:gpt-review
orch:approved
orch:rework
orch:blocked
```

Rules:

1. Resolve dependency trong cùng `issues_repo` của managed project.
2. Dependency phải tồn tại duy nhất và `issue_id` trong body phải khớp title.
3. Self dependency bị reject.
4. Cycle như `issue1 -> issue2 -> issue1` bị reject/fail closed.
5. Dependency missing/ambiguous/invalid làm task không claimable và phải có diagnostic rõ ràng.
6. Task chưa đủ condition ở state `WAITING_CONDITION`, lifecycle label `orch:waiting-condition`.
7. Mỗi lần sync/reconciliation phải đánh giá lại dependencies.
8. Khi dependency cuối cùng chuyển DONE, Orchestrator tự:
   `WAITING_CONDITION -> READY`,
   đổi label sang `orch:ready`,
   và task trở thành queue candidate ngay, không cần user/GPT gửi retry.
9. Các task `condition: []` có thể chạy song song nếu bounded worker capacity cho phép.
10. Thay đổi `condition` hoặc `issue_id` là task contract mutation, bắt buộc tăng `revision`.

### Reviewer

Cho phép:

```json
"review": {
  "reviewer": "GPT"
}
```

hoặc:

```json
"review": {
  "reviewer": "AGY"
}
```

hoặc future mode:

```json
"review": {
  "reviewer": ["AGY", "GPT"]
}
```

Nếu reviewer có `GPT`, task không được DONE trước khi có GPT PASS bound đúng HEAD SHA.

---

## 7. Label lifecycle chuẩn

Recommended labels:

```text
orch:waiting-condition
orch:ready
orch:running
orch:question
orch:user-gate
orch:rework
orch:gpt-review
orch:approved
orch:blocked
orch:done
```

Mapping:

| State | Label |
|---|---|
| Chờ dependency | `orch:waiting-condition` |
| Task sẵn sàng | `orch:ready` |
| AGY đang làm | `orch:running` |
| Chờ trả lời | `orch:question` |
| Chờ user approval | `orch:user-gate` |
| GPT yêu cầu fix | `orch:rework` |
| Chờ GPT review | `orch:gpt-review` |
| GPT PASS nhưng merge đang bị chặn kỹ thuật | `orch:approved` |
| Không thể tiếp tục | `orch:blocked` |
| Merge/complete | `orch:done` |

Một Issue chỉ có **một lifecycle label chính** tại một thời điểm.

---

## 8. Cách Orchestrator tìm Issue để xử lý

### 8.1. Startup discovery

Sau khi dashboard/bootstrap/sync sẵn sàng:

1. Đọc tất cả managed projects trong registry.
2. Với mỗi project, lấy `issues_repo`.
3. Query GitHub:
   - `state=open`;
   - có label bắt đầu bằng `orch:`.
4. Parse Issue title và body.
5. Reject/fail closed nếu:
   - không có canonical `[issue<ID>]`;
   - thiếu `orchestrator-task`;
   - project không match registry;
   - revision invalid;
   - target repo mismatch.
6. Build local issue index.

### 8.2. Queue candidates

Task executor chỉ claim:

```text
state = open
label = orch:ready
task contract valid
all condition dependencies satisfied
not already leased
worker capacity available
```

Nếu task contract hợp lệ nhưng `condition` chưa thỏa:

```text
state = WAITING_CONDITION
label = orch:waiting-condition
DO NOT CLAIM
```

Khi dependency cuối cùng DONE, reconciliation tự chuyển task sang `orch:ready`.

Ordering recommended:

1. `priority` nhỏ hơn trước;
2. Issue created_at cũ hơn trước;
3. GitHub Issue number nhỏ hơn trước.

---

## 9. Continuous sync

Orchestrator phải sync GitHub Issues liên tục.

Recommended interval:

```text
5–15 seconds
```

### 9.1. Mỗi vòng sync

Cho mỗi open Issue đã biết:

1. Fetch Issue metadata:
   - state;
   - labels;
   - updated_at;
   - body revision.
2. Nếu `updated_at` thay đổi:
   - re-fetch body;
   - parse task contract;
   - compute task hash.
3. Fetch comments **sau cursor cuối cùng**.
4. Parse từng event mới theo thứ tự comment ID.
5. Ghi cursor sau khi xử lý thành công.
6. Reconcile PR:
   - PR number;
   - branch;
   - current head SHA;
   - merge state.
7. Reconcile local lease/worktree.
8. Re-evaluate every task's `condition` graph.
9. Move newly satisfied tasks from `orch:waiting-condition` to `orch:ready` automatically.

### 9.2. Cursor

Phải lưu:

```text
last_comment_id
last_issue_updated_at
last_task_revision
last_task_hash
last_reviewed_head_sha
last_review_cycle
```

Không được mỗi vòng đọc xong rồi xử lý lại toàn bộ command cũ.

---

## 10. Idempotency và chống replay

Mỗi structured event có:

```text
event_id
issue_id
revision
actor
event
```

Orchestrator lưu `event_id` đã consume.

Nếu event xuất hiện lại:

```text
event_id already consumed
→ IGNORE
```

Không được:

- retry cùng một answer cũ;
- áp dụng lại GPT PASS cũ;
- áp dụng lại gate approval cũ;
- replay fix request cũ.

---

## 11. Revision rule

Task contract có `revision`.

Ví dụ:

```text
revision 1 → initial task
revision 2 → GPT bổ sung acceptance
revision 3 → user đổi rule
```

Nếu Issue body thay đổi nhưng revision không tăng:

```text
FAIL CLOSED
```

Orchestrator comment:

```text
[issue1_blocked_byORCH]

reason_code: SAME_REVISION_MUTATION

Task body changed without revision increment.
```

---

## 12. Question / Answer flow

### AGY hỏi

```text
[issue1_question_byAGY]

question_id: q-001
...
```

State:

```text
RUNNING → WAITING_ANSWER
label → orch:question
```

### GPT/User trả lời

```text
[issue1_answer_byGPT]

question_id: q-001
revision: 4
...
```

Orchestrator chỉ accept answer nếu:

- đúng Issue;
- đúng revision;
- đúng `question_id`;
- comment chưa consume;
- author được allow.

Sau đó:

```text
WAITING_ANSWER → RUNNING
```

---

## 13. AGY handoff flow

Khi Orchestrator claim Issue:

1. Verify the task's `condition` is fully satisfied and acquire an atomic per-task worker lease.
2. Sync target repository.
3. Load:
   - Issue contract;
   - all relevant comments;
   - project rules;
   - plans;
   - agent profile;
   - task profile;
   - Graphify context.
4. Create/resume same task branch/worktree.
5. Build AGY handoff packet.

Recommended handoff packet:

```json
{
  "issue_id": "issue1",
  "github_issue_number": 42,
  "revision": 4,
  "task": { "...": "..." },
  "conversation": [
    {
      "event": "question",
      "actor": "AGY",
      "question_id": "q-001"
    },
    {
      "event": "answer",
      "actor": "GPT",
      "question_id": "q-001"
    }
  ],
  "branch": "task/issue-42-game-0001",
  "pr": 17,
  "review_cycle": 2
}
```

AGY không cần tự crawl GitHub nếu Orchestrator đã handoff packet đầy đủ.

---

## 14. Fix loop

### 14.1. AGY xử lý xong

AGY/Orchestrator comment:

```text
[issue1_fixdone_byAGY]
...
head_sha: def456
```

Nếu reviewer = GPT:

```text
label → orch:gpt-review
state → WAITING_GPT_REVIEW
```

### 14.2. GPT yêu cầu fix

GPT comment:

```text
[issue1_review_fix_byGPT]
...
head_sha: def456
findings: ...
```

Orchestrator:

```text
WAITING_GPT_REVIEW
→ REWORK
label → orch:rework
→ handoff SAME ISSUE + SAME BRANCH + SAME PR to AGY
```

Không tạo Issue mới.

### 14.3. AGY fix xong lần nữa

```text
[issue1_fixdone_byAGY]

review_cycle: 3
head_sha: xyz789
```

Sau đó lại `orch:gpt-review`.

---

## 15. ChatGPT command: /review

Khi user gõ trong ChatGPT:

```text
/review
```

GPT phải hiểu đây là **review queue command**, không phải chỉ review conversation hiện tại.

### 15.1. Discovery

GPT phải tìm trên tất cả managed project repos:

```text
state = open
label = orch:gpt-review
review.reviewer contains GPT
```

Nếu có nhiều Issue:

- review tất cả Issue đang chờ GPT;
- thứ tự theo priority/task queue;
- không bỏ qua Issue cũ chỉ vì chat hiện tại đang nói project khác.

### 15.2. Dữ liệu GPT phải đọc

Cho mỗi Issue:

1. Issue body/task contract.
2. Revision hiện tại.
3. Toàn bộ communication comments liên quan.
4. Latest `fixdone_byAGY`.
5. PR.
6. PR current HEAD SHA.
7. Diff thực tế.
8. Tests/evidence.
9. AGY internal review.
10. Project rules/plans cần thiết.

GPT không được PASS chỉ dựa vào:

```text
"AGY says 100/100"
```

Phải kiểm chứng code/artifact/diff thực tế.

---

## 16. GPT review binding

Mỗi GPT review phải bind vào:

```text
issue_id
revision
review_cycle
pr
head_sha
```

Ví dụ PASS:

```text
[issue1_review_pass_byGPT]

revision: 4
review_cycle: 3
pr: 17
head_sha: xyz789
```

Nếu AGY push thêm commit sau PASS:

```text
current_head_sha != reviewed_head_sha
→ PASS INVALIDATED
→ orch:gpt-review
```

Không được merge dựa trên stale review.

---

## 17. Phản hồi review ở đâu?

**Mặc định luôn comment vào canonical Issue gốc.**

Ví dụ:

```text
Issue #42
[issue1] Fix Foundation movement
```

GPT review lỗi:

```text
comment vào Issue #42:
[issue1_review_fix_byGPT]
...
```

AGY sửa:

```text
comment vào Issue #42:
[issue1_fixdone_byAGY]
...
```

GPT PASS:

```text
comment vào Issue #42:
[issue1_review_pass_byGPT]
...
```

### Không tạo Issue mới nếu

- test fail trong scope hiện tại;
- implementation sai acceptance;
- thiếu test;
- UI sai spec;
- regression do task;
- asset chưa đạt;
- reviewer evidence chưa đủ.

### Chỉ tạo Issue mới nếu

Phát hiện việc **ngoài scope**:

- bug độc lập;
- architecture debt không nên block task hiện tại;
- enhancement mới;
- cross-project problem;
- follow-up feature.

Khi đó canonical Issue hiện tại comment:

```text
[issue1_followup_created_byGPT]

followup_issue: #55
reason: Out-of-scope architecture issue discovered during review.
```

---

## 18. Merge / Done — GPT là final gate

Khi reviewer = GPT, `/review` PASS phải thực hiện merge và resolve ngay trong cùng transaction logic.

### 18.1. Success path

Sau khi GPT đã verify exact PR HEAD:

```text
[issue1_review_pass_byGPT]
        ↓
re-fetch PR
        ↓
current_head_sha == reviewed_head_sha ?
        │
       YES
        ↓
merge PR with expected_head_sha
        ↓
[issue1_done_byGPT]
        ↓
label → orch:done
Issue → closed(completed)
release task worker lease
```

Rules:

1. GPT phải re-fetch PR ngay trước merge.
2. Merge chỉ được phép nếu current HEAD khớp chính xác HEAD đã review.
3. Merge request phải dùng `expected_head_sha` để chống race/stale review.
4. Sau merge thành công, comment `[issueX_done_byGPT]` phải chứa `revision`, `review_cycle`, PR number, reviewed HEAD SHA và merge SHA.
5. Sau đó đặt `orch:done` và close Issue as completed.
6. Không dừng ở `orch:approved` nếu merge thành công.

### 18.2. Merge blocked path

Nếu GPT PASS nhưng merge bị chặn bởi conflict, required check, branch protection hoặc permission:

```text
[issue1_merge_blocked_byGPT]
label → orch:approved
Issue remains open
```

Sau khi blocker được xử lý:

- re-fetch PR;
- nếu HEAD vẫn đúng reviewed HEAD → merge được phép;
- nếu HEAD đã đổi → GPT PASS cũ invalid, chuyển lại `orch:gpt-review` và review lại.

`orch:approved` vì vậy chỉ là trạng thái tạm khi **review đã PASS nhưng merge chưa thể hoàn tất về mặt kỹ thuật**.

---

## 19. Open Issue reconciliation

Orchestrator phải định kỳ tìm lại toàn bộ open Issues, không chỉ dựa vào local DB.

Mục tiêu:

- recover khi local DB mất;
- nhận task do GPT tạo khi orchestrator offline;
- nhận comment mới;
- nhận label được sửa thủ công;
- nhận PR merge từ bên ngoài.

Recommended:

```text
full open-issue reconciliation: mỗi 1–5 phút
incremental comment sync: mỗi 5–15 giây
```

Local DB là cache/runtime state, **không phải source of truth**.

---

## 20. Closed Issue reconciliation

Closed Issue không được execute lại.

Nếu local lease trỏ vào Issue đã closed:

```text
stop execution
mark local task cancelled/closed
release lease
```

Nếu PR đã merge nhưng Issue chưa close:

```text
reconcile
post issue_done_byORCH
close Issue
```

---

## 21. State machine tổng thể

```text
                         ┌───────────────┐
                         │   NEW ISSUE   │
                         │ [issue1] ...  │
                         └───────┬───────┘
                                 │
                    condition satisfied?
                         ┌───────┴────────┐
                         │ NO             │ YES
                         ▼                ▼
                  WAIT_CONDITION      orch:ready
                         │                │
                   dependency DONE       │
                         └────────────────┘
                                          ▼
                         ┌───────────────┐
                         │    RUNNING    │
                         │ AGY executes  │
                         └───┬─────┬─────┘
                             │     │
                    question│     │user gate
                             ▼     ▼
                      WAIT_ANSWER WAIT_GATE
                             │     │
                             └──┬──┘
                                ▼
                             RUNNING
                                │
                                ▼
                      [fixdone_byAGY]
                                │
                                ▼
                        GPT_REVIEW
                         /review
                          │    │
               FIX_REQUIRED    PASS
                    │           │
                    ▼           ▼
                  REWORK       MERGE
                    │           │
                    └─AGY───────┘
                                │
                     ┌──────────┴──────────┐
                     │ success             │ blocked
                     ▼                     ▼
                    DONE               APPROVED
                                           │
                                  resolve blocker / recheck HEAD
                                           │
                                           └────→ MERGE
```

---

## 22. Example end-to-end

### Step 1 — GPT creates task

Issue title:

```text
[issue1] Fix Foundation movement
```

Label:

```text
orch:ready
```

### Step 2 — Orchestrator claims task

Comment:

```text
[issue1_started_byAGY]

revision: 1
branch: task/issue-42-game-0001
```

### Step 3 — AGY asks question

```text
[issue1_question_byAGY]

question_id: q1

Should pursuit speed scale with distance?
```

### Step 4 — GPT answers

```text
[issue1_answer_byGPT]

question_id: q1
revision: 1

Yes. Use bounded scaling.
```

### Step 5 — AGY finishes

```text
[issue1_fixdone_byAGY]

revision: 1
review_cycle: 1
pr: 17
head_sha: abc123
```

Issue label:

```text
orch:gpt-review
```

### Step 6 — user types in ChatGPT

```text
/review
```

GPT finds Issue #42 and checks PR #17.

### Step 7 — GPT finds bug

```text
[issue1_review_fix_byGPT]

revision: 1
review_cycle: 1
pr: 17
head_sha: abc123

Finding:
Rear pursuit speed is not capped.
```

Label:

```text
orch:rework
```

### Step 8 — AGY fixes same Issue/branch/PR

```text
[issue1_fixdone_byAGY]

revision: 1
review_cycle: 2
pr: 17
head_sha: def456
```

### Step 9 — /review again

GPT PASS:

```text
[issue1_review_pass_byGPT]

revision: 1
review_cycle: 2
pr: 17
head_sha: def456

Verdict: PASS
```

### Step 10 — GPT merges and resolves

GPT re-fetches PR #17, verifies HEAD is still `def456`, and merges with `expected_head_sha=def456`.

```text
[issue1_done_byGPT]

revision: 1
review_cycle: 2
pr: 17
reviewed_head_sha: def456
merge_sha: 999aaa
```

Then:

```text
label → orch:done
Issue → closed(completed)
```

---

## 23. AGY implementation requirements

AGY phát triển communication subsystem phải có ít nhất:

1. Canonical Issue parser.
2. Event-header parser.
3. Structured event JSON parser.
4. Comment cursor.
5. Consumed-event store.
6. Task revision/hash validation.
7. Question correlation.
8. User-gate correlation.
9. GPT review HEAD/review-cycle correlation.
10. Open Issue full reconciliation.
11. Incremental comment sync.
12. Same-Issue/same-branch/same-PR rework.
13. Recovery after restart.
14. Closed/merged Issue reconciliation.
15. Dashboard visibility of communication state.
16. Event audit log.
17. Tests for stale/replayed/out-of-order events.
18. Task `issue_id` validation against canonical title.
19. Dependency graph parsing and cycle detection.
20. WAITING_CONDITION lifecycle and automatic READY transition.
21. Bounded parallel scheduling for independent `condition: []` tasks.

---

## 24. Required tests

Minimum automated tests:

- new `[issue1]` discovered from open Issues;
- invalid title rejected;
- missing task contract rejected;
- comments after cursor only;
- duplicate event ignored;
- old revision ignored;
- same-revision body mutation blocked;
- question answer requires exact `question_id`;
- `fixdone_byAGY` moves GPT-reviewed task to `orch:gpt-review`;
- `review_fix_byGPT` moves same task to rework;
- rework uses same Issue/branch/PR;
- `review_pass_byGPT` requires exact HEAD SHA;
- GPT PASS immediately attempts merge using `expected_head_sha`;
- successful GPT merge posts `done_byGPT`, sets `orch:done`, and closes Issue;
- merge blocker after PASS produces `merge_blocked_byGPT` and leaves Issue open;
- HEAD change after PASS invalidates approval and requires re-review;
- new commit invalidates old GPT PASS;
- restart recovers open task;
- closed Issue is not executed;
- merged PR reconciles to DONE;
- /review discovery returns every open `orch:gpt-review` Issue assigned to GPT;
- title `[issue7]` must match task contract `issue_id: issue7`;
- missing dependency prevents claim;
- dependency on non-DONE Issue prevents claim;
- dependency becomes DONE and dependent task automatically becomes READY;
- `condition: []` task is immediately eligible;
- self dependency is rejected;
- dependency cycle is rejected;
- independent ready tasks may execute in parallel up to configured worker capacity.

---

## 25. Source-of-truth priority

Nếu dữ liệu conflict, ưu tiên:

1. Current canonical Issue body + revision.
2. Valid structured Issue comments.
3. Current PR + HEAD SHA + diff.
4. Project rules/plans.
5. Local runtime DB/cache.
6. Dashboard presentation.

Local cache không được override GitHub source of truth.

---

## 26. Final communication rule

Rule ngắn gọn nhất:

```text
NEW WORK      → create a new canonical Issue
SAME WORK     → communicate in the same Issue
AGY FINISHED  → [issueX_fixdone_byAGY]
GPT NEED FIX  → [issueX_review_fix_byGPT]
GPT PASS      → [issueX_review_pass_byGPT] → merge immediately
MERGE BLOCKED → [issueX_merge_blocked_byGPT]
QUESTION      → [issueX_question_byAGY]
ANSWER        → [issueX_answer_byGPT]
DONE          → [issueX_done_byGPT] → orch:done → close Issue
```

**Một task không được phân mảnh thành nhiều Issue chỉ để biểu diễn trạng thái.**

## 27. Automatic GPT review trigger and bidirectional task delegation

### 27.1. GitHub review trigger signal

The canonical GitHub trigger condition for GPT review is:

```text
Issue = OPEN
label = orch:gpt-review
task.review.reviewer contains GPT
```

This condition is equivalent to the user manually typing `/review`.

A ChatGPT-side condition watcher may poll all enabled managed-project `issues_repo` values and invoke the full GPT review flow when the condition becomes true.

Important limitation: GitHub Issues/labels are the trigger **signal**, but ChatGPT does not expose a generic inbound webhook endpoint for GitHub to invoke directly. Therefore a ChatGPT automation may use periodic condition polling. If a true immediate webhook is required, it must be implemented by an external service/API integration rather than assumed by this protocol.

### 27.2. Automatic review behavior

When the trigger is detected:

```text
orch:gpt-review
      ↓
GPT fetches canonical Issue + latest fixdone + PR + exact HEAD + diff + tests
      ↓
FIX_REQUIRED ──→ same Issue → orch:rework
      │
      └ PASS ──→ verify HEAD again → merge → done_byGPT → orch:done → close
```

No separate "review request" Issue is created.

### 27.3. GPT → Orchestrator/AGY reverse delegation

GPT may assign work back to the local Orchestrator by creating a new canonical Issue when:

- the user explicitly asks GPT to delegate a new task;
- review discovers a genuinely out-of-scope bug/debt/follow-up;
- an approved plan defines a next executable work package.

The new task must:

1. allocate the next logical `issueN`;
2. contain an `orchestrator-task` block;
3. declare `condition` dependencies explicitly;
4. use `orch:ready` only when dependencies are satisfied, otherwise `orch:waiting-condition`;
5. set the intended final reviewer explicitly.

If a follow-up was discovered during review, link it back from the original Issue with:

```text
[issueX_followup_created_byGPT]

followup_issue: #<number>
reason: <why this is outside the current task scope>
```

In-scope defects MUST NOT create a new Issue; they stay in the original Issue as `review_fix_byGPT`.

