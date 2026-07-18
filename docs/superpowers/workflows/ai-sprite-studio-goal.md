# AI Sprite Studio `/goal` Subagent Playbook

**Target file:** `docs/superpowers/workflows/ai-sprite-studio-goal.md`

## Purpose

Execute the approved AI Sprite Studio plan continuously through isolated, reviewed subagent tasks while preserving progress across context compaction or interrupted sessions.

This playbook uses built-in goal tracking, collaboration agents, Git, and existing Superpowers scripts. It does not introduce a custom orchestrator.

## Canonical configuration

```text
PLAN_FILE=docs/superpowers/plans/2026-07-17-ai-sprite-studio.md
PLAYBOOK_FILE=docs/superpowers/workflows/ai-sprite-studio-goal.md
FEATURE_BRANCH=feature/ai-sprite-studio-v1
WORKTREE_PATH=.worktrees/ai-sprite-studio-v1
LEDGER_FILE=.superpowers/sdd/progress.md
SDD_SKILL_DIR=/home/ops/.codex/superpowers/skills/subagent-driven-development
BASE_BRANCH=main
```

Only the controller may update the goal, ledger, or task sequencing.

## Goal command

Start execution with:

```text
/goal Implement AI Sprite Studio v1 exactly as specified in docs/superpowers/plans/2026-07-17-ai-sprite-studio.md. Use docs/superpowers/workflows/ai-sprite-studio-goal.md as the orchestration contract. Work in an isolated feature/ai-sprite-studio-v1 worktree. Execute every task with a fresh implementer, behavior-first TDD, independent spec-and-quality review, fix/re-review loops, durable progress tracking, final whole-branch review, and fresh end-to-end verification. Do not merge, push, publish, delete work, or spend OpenAI/Higgsfield credits without explicit authorization. Complete only after provider-free tests/build/E2E and authorized live provider smoke tests pass.
```

API equivalent:

```json
{
  "objective": "Implement AI Sprite Studio v1 exactly as specified in docs/superpowers/plans/2026-07-17-ai-sprite-studio.md using the checked-in orchestration playbook, isolated worktree, behavior-first TDD, fresh task agents, independent reviews, durable progress, final review, and full verification. Do not merge, push, publish, delete work, or spend provider credits without explicit authorization."
}
```

Do not set a token budget.

## Orchestration topology

```text
/root goal controller
  │
  ├─ task_N implementer
  │    └─ writes tests and implementation, verifies, commits, reports
  │
  ├─ review_N reviewer
  │    └─ read-only spec and quality review
  │
  ├─ task_N implementer/fixer
  │    └─ resolves blocking findings and appends evidence
  │
  ├─ review_N reviewer
  │    └─ re-review until clean
  │
  ├─ next task
  │
  ├─ final_branch_review
  │    └─ whole-plan and cross-task review
  │
  ├─ one final fix wave if necessary
  │
  └─ verification → goal completion → branch handoff
```

Agents share one filesystem. Never run two writing agents concurrently. Parallel agents are allowed only for independent, read-only investigations.

## Roles

### Goal controller

The root agent:

- Reads the goal, plan, ledger, and Git history.
- Creates or resumes the isolated worktree.
- Extracts task briefs and review packages.
- Dispatches agents with `fork_turns: "none"`.
- Answers implementer questions.
- Verifies commits and test evidence.
- Runs fresh task verification before advancing.
- Updates the durable ledger.
- Owns provider authorization and branch handoff.
- Marks the goal complete or blocked.

The controller does not manually implement task fixes.

### Implementer

A fresh implementer handles exactly one numbered task.

Requirements:

- Read its task brief first.
- Read only named interface files from earlier tasks.
- Use behavior-first red-green-refactor.
- Use focused verification for copied assets, documentation, configuration, and lockfiles.
- Reuse `sprite-gen`, stdlib, and installed dependencies before adding code.
- Do not add speculative abstractions or unrelated refactors.
- Do not invoke paid providers.
- Run task-specific tests.
- Self-review the diff.
- Commit the task.
- Write a durable report.

Allowed statuses:

```text
DONE
DONE_WITH_CONCERNS
NEEDS_CONTEXT
BLOCKED
```

### Task reviewer

A fresh reviewer receives only the task brief, implementer report, review package, and global constraints.

The reviewer is read-only and returns:

```text
Spec compliance: APPROVED | REJECTED
Code quality: APPROVED | REJECTED
```

Findings are categorized as:

```text
Critical  — security, data loss, fundamentally broken behavior
Important — missing requirement, incorrect behavior, missing essential test
Minor     — non-blocking maintainability or polish issue
```

### Fixer

Use the original implementer through `followup_task` when available. Otherwise spawn one fresh fixer.

The fixer:

- Receives all Critical and Important findings together.
- Changes only what those findings require.
- Reruns covering tests.
- Appends commands and results to the original report.
- Commits the fix.

### Final reviewer

A fresh reviewer examines the complete merge-base-to-HEAD package against the entire plan. It focuses on cross-task integration, security boundaries, manifest consistency, provider authorization, data-loss risks, and unnecessary complexity.

If findings remain, dispatch one fixer with the complete findings list.

## Task dependency graph

```text
1 Bootstrap and upstream lock
└─ 2 Contracts and atomic project storage
   └─ 3 Local server and persistent job runner
      └─ 4 Prompt registry and pipeline DAG
         └─ 5 Canonical sprite engine and upload-only flow
            ├─ 6 OpenAI generation and editing
            │  └─ 7 Directions and action poseboards
            │     └─ 8 Curator and AI frame editing
            │        └─ 9 Higgsfield walk pipeline
            │           └─ 10 Contextual chat orchestration
            └───────────────└─ 11 QA and export
                                  └─ 12 Packaging and E2E acceptance
```

Execution remains sequential because later tasks consume interfaces and files created by earlier tasks.

| Task | Agent name | Required predecessors |
|---:|---|---|
| 1 | `task_01_bootstrap` | None |
| 2 | `task_02_storage` | 1 |
| 3 | `task_03_server_jobs` | 2 |
| 4 | `task_04_prompt_pipeline` | 2–3 |
| 5 | `task_05_sprite_engine` | 4 |
| 6 | `task_06_openai` | 3–5 |
| 7 | `task_07_directions_actions` | 5–6 |
| 8 | `task_08_curator` | 5–7 |
| 9 | `task_09_higgsfield` | 3, 5, 8 |
| 10 | `task_10_chat` | 3–9 |
| 11 | `task_11_qa_export` | 5–10 |
| 12 | `task_12_release` | 1–11 |

## Phase 0: Goal and repository preflight

### 0.1 Resume detection

Call `get_goal`, then inspect:

```bash
git status --short
git branch --show-current
git log --oneline -10
git rev-parse --git-dir
git rev-parse --git-common-dir
```

If the ledger exists, read it:

```bash
cat .superpowers/sdd/progress.md
```

Tasks marked complete in the ledger must not be dispatched again.

### 0.2 Persist requirements

Ensure the approved plan and this playbook exist at their canonical paths. Commit them before feature implementation.

### 0.3 Plan conflict scan

Before Task 1, scan once for contradictions involving:

- One canonical pixel engine.
- Local single-user deployment.
- Provider spending approvals.
- Immutable candidate history.
- Sequential writing agents.
- Behavior-first TDD.
- Public contracts shared between tasks.
- Ponytail/YAGNI constraints.

Present all genuine contradictions in one batched user question. If none exist, continue without interruption.

### 0.4 Worktree setup

Detect existing worktree isolation first. If already isolated, use it.

Otherwise ensure these entries are ignored:

```gitignore
.worktrees/
.superpowers/
```

Commit the ignore change, then create:

```bash
git worktree add .worktrees/ai-sprite-studio-v1 -b feature/ai-sprite-studio-v1
```

All subsequent commands run from the created worktree.

Never create a nested worktree or remove a harness-owned worktree.

### 0.5 Baseline

Record:

```bash
git status --short
git rev-parse HEAD
```

Run the existing test command if the repository has one. If a pre-existing test fails, stop and ask whether to investigate or continue with the known failure.

Initialize the ledger:

```text
Goal: AI Sprite Studio v1
Branch: feature/ai-sprite-studio-v1
Merge base: <sha>
Current task: 1
Minor findings:
External gates:
- OpenAI live smoke: pending authorization
- Higgsfield live smoke: pending authorization
```

## Phase 1: Per-task execution loop

### 1.1 Extract the task brief

Run:

```bash
/home/ops/.codex/superpowers/skills/subagent-driven-development/scripts/task-brief \
  docs/superpowers/plans/2026-07-17-ai-sprite-studio.md \
  <TASK_NUMBER>
```

The command prints a unique brief path. Derive the report path from it:

```text
.../task-N-brief.md
.../task-N-report.md
```

Record the task base:

```bash
git rev-parse HEAD
```

Never use `HEAD~1` as the review base.

### 1.2 Dispatch implementer

Call the collaboration tool directly, not through a shell:

```text
spawn_agent({
  task_name: "task_N_slug",
  fork_turns: "none",
  message: "<implementer handoff below>"
})
```

Implementer handoff:

```text
You are the implementer for Task <N>: <TASK_NAME>.

This task contributes: <ONE_SENTENCE_PRODUCT_ROLE>.

Read <TASK_BRIEF_PATH> first. It is your complete requirement and its exact
values govern. Read only the named interface files needed from prior tasks.

Global constraints:
- Python 3.12 local single-user web application.
- One canonical pixel engine: pinned sprite-gen.
- Preserve raw artifacts and accepted candidates.
- No paid provider calls.
- No speculative abstractions, unrelated refactors, or unrequested dependencies.
- Only one writing agent is active.
- Never reset, discard, or overwrite unrelated user work.

Execution contract:
1. For application behavior, write the smallest failing test first.
2. Run it and confirm it fails for the expected missing behavior.
3. Implement the minimum code that passes.
4. Run the focused tests and relevant neighbouring tests.
5. Refactor only while green.
6. Verify copied assets/config/docs with focused checks.
7. Inspect git diff and self-review.
8. Commit the finished task.
9. Write the report to <TASK_REPORT_PATH>.

Report contents:
- status
- commit SHAs
- files changed
- RED command and failure evidence
- GREEN command and passing evidence
- self-review
- concerns or blockers

Return only:
STATUS: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED
COMMITS: <sha list>
TESTS: <one-line result>
CONCERNS: <one line or none>

Do not spawn child agents.
```

### 1.3 Wait and monitor

Use:

```text
wait_agent({ timeout_ms: 30000 })
```

Do not spawn a replacement merely because an agent has not returned. Use `list_agents` to inspect status.

Use `interrupt_agent` only if the agent is performing an unsafe or clearly out-of-scope action.

### 1.4 Handle implementer status

| Status | Controller action |
|---|---|
| `DONE` | Verify report and commits, then review. |
| `DONE_WITH_CONCERNS` | Read concerns; resolve correctness concerns before review. |
| `NEEDS_CONTEXT` | Send only missing context with `followup_task`. |
| `BLOCKED` | Supply context, split the task, or expose the plan defect to the user. |

Follow-up example:

```text
followup_task({
  target: "task_N_slug",
  message: "Read <INTERFACE_PATH>. The locked decision is <DECISION>. Continue the same task and update <REPORT_PATH>."
})
```

### 1.5 Controller evidence check

Before review:

```bash
git status --short
git log --oneline <TASK_BASE_SHA>..HEAD
```

Confirm:

- Report exists.
- Commits exist.
- Files are within task scope.
- Test commands and output are present.
- No unrelated changes were overwritten.

Agent success claims are not sufficient evidence.

## Phase 2: Per-task review gate

### 2.1 Create review package

Run:

```bash
/home/ops/.codex/superpowers/skills/subagent-driven-development/scripts/review-package \
  <TASK_BASE_SHA> \
  HEAD
```

Use the unique path printed by the command.

### 2.2 Dispatch reviewer

```text
spawn_agent({
  task_name: "review_N_slug",
  fork_turns: "none",
  message: "<reviewer handoff below>"
})
```

Reviewer handoff:

```text
You are the read-only reviewer for Task <N>: <TASK_NAME>.

Read:
1. <TASK_BRIEF_PATH>
2. <TASK_REPORT_PATH>
3. <REVIEW_PACKAGE_PATH>

Binding global constraints:
<VERBATIM GLOBAL CONSTRAINTS FROM PLAN>

Do not edit files. Do not repeat tests already evidenced in the report.

Review for:
- exact spec compliance
- missing or extra behavior
- security and data-loss risks
- incorrect interfaces or state transitions
- untested meaningful branches
- duplicated sprite-gen or stdlib behavior
- speculative abstractions and unnecessary dependencies
- unrelated changes
- mismatch between report and diff

Return:
Spec compliance: APPROVED | REJECTED
Code quality: APPROVED | REJECTED

Critical:
- <location, problem, remedy>

Important:
- <location, problem, remedy>

Minor:
- <location, problem, remedy>

Cannot verify:
- <requirement and reason>
```

### 2.3 Review decision

A task advances only when both verdicts are `APPROVED`.

- Critical and Important findings block advancement.
- Minor findings enter the ledger.
- `Cannot verify` items must be resolved by the controller before advancement.
- Findings that contradict the approved plan require a user decision.

### 2.4 Fix loop

Send all blocking findings together:

```text
followup_task({
  target: "task_N_slug",
  message: "The task review found the following blocking issues:\n<FINDINGS>\nFix all issues, rerun <COVERING_TEST_FILES>, commit, and append commands/results to <TASK_REPORT_PATH>. Do not add unrelated changes."
})
```

If the original implementer is unavailable:

```text
spawn_agent({
  task_name: "fix_N_slug",
  fork_turns: "none",
  message: "<same fix contract plus paths>"
})
```

After the fix:

1. Confirm the covering test evidence.
2. Regenerate the review package using the original task base.
3. Re-dispatch the reviewer.
4. Repeat until both verdicts approve.

Never use `git reset --hard` or discard uncommitted work to repair scope mistakes. Prefer a focused corrective commit or recoverable `git revert`.

## Phase 3: Task completion gate

The controller runs the task’s focused verification command freshly.

Only then append:

```text
Task <N>: complete (commits <BASE>..<HEAD>, review clean, verification passed)
```

Update `Current task` to the next incomplete task.

Continue automatically through Tasks 1–12.

## Safe parallel investigation

Parallelism is allowed only when two or more failures have independent root causes and agents can remain read-only.

Dispatch up to three investigators:

```text
spawn_agent({
  task_name: "investigate_<domain>",
  fork_turns: "none",
  message: "Investigate failures in <TEST_FILE_OR_SUBSYSTEM>. Do not edit or commit. Identify the root cause, all shared callers, and the smallest correct fix. Write findings to <REPORT_PATH> and return one-line status."
})
```

After investigations:

1. Compare root causes.
2. Resolve overlapping recommendations.
3. Dispatch one fixer to apply the combined fix sequentially.
4. Run the complete affected suite.

Never dispatch parallel implementers into the shared worktree.

## Provider authorization gate

Before any paid provider invocation, show:

```text
Provider:
Model/tool:
Request count:
Image dimensions/quality or video duration:
Directions/states:
New request or retry:
Known provider request IDs:
Estimated provider-side cost/credit impact:
```

The user must explicitly authorize the call.

Rules:

- OpenAI and Higgsfield approvals are separate.
- Never infer approval from an earlier unrelated generation.
- Never silently switch providers.
- Never automatically retry a request with unknown billing status.
- Persist returned provider request IDs immediately.
- A crashed, ambiguous paid call becomes `attention_required`.

Task 9 completes its mocked MCP integration before requesting Higgsfield OAuth.

## Recovery rules

### Conversation compaction or session restart

1. Call `get_goal`.
2. Read `.superpowers/sdd/progress.md`.
3. Inspect `git log` and `git status`.
4. Resume the first task not marked complete.
5. Do not re-dispatch completed tasks.

### Agent timeout

- Check `list_agents`.
- Wait again if still active.
- Interrupt only for unsafe drift.
- Do not create a duplicate writer.

### Missing context

Use `followup_task` with the smallest missing interface or decision. Do not paste the full conversation or plan.

### Task too large

Split it into sequential subtasks such as `9a`, `9b`, and `9c`, but retain one task review covering the complete original Task 9 range.

### Test failure

- Reproduce with the smallest command.
- Write or confirm a regression test.
- Fix the shared root cause.
- Rerun focused and neighbouring tests.
- Never merely increase timeouts or weaken assertions.

### Unexpected unrelated changes

Preserve them. If they overlap the task and cannot be safely separated, stop and request direction.

### Provider outage

Continue provider-free work. Use mocks and manual-upload fallbacks. Keep the live acceptance gate open.

### Repeated external blocker

Do not mark blocked on the first occurrence. If the same blocker prevents meaningful progress for three consecutive goal turns, call:

```json
{
  "status": "blocked"
}
```

### Goal completion

Never mark complete because time, context, or token budget is running low.

## Phase 4: Whole-branch review

After Task 12:

```bash
git merge-base main HEAD
```

Generate the final review package:

```bash
/home/ops/.codex/superpowers/skills/subagent-driven-development/scripts/review-package \
  <MERGE_BASE_SHA> \
  HEAD
```

Dispatch:

```text
spawn_agent({
  task_name: "final_branch_review",
  fork_turns: "none",
  message: "Read the complete approved plan, progress ledger, final implementation report, and <FINAL_REVIEW_PACKAGE>. Perform a read-only whole-branch review for cross-task correctness, missing requirements, security, data loss, provider authorization, manifest/export compatibility, unnecessary complexity, and test gaps. Return spec and quality verdicts with Critical/Important/Minor findings."
})
```

If findings exist, send the complete list to one fixer. Repeat final review after the fix.

## Phase 5: Final verification

Run freshly:

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run python scripts/sync_upstreams.py --check
uv build
uv run pytest tests/e2e/test_studio.py -q
uv run ai-sprite-studio doctor --offline
git status --short
```

Install the wheel in a temporary environment and run its offline doctor.

With separate user authorization:

```bash
RUN_LIVE_OPENAI=1 uv run pytest tests/live/test_openai.py -q
RUN_LIVE_HIGGSFIELD=1 uv run pytest tests/live/test_higgsfield.py -q
```

Completion requires:

- All 12 ledger entries complete.
- Every task review approved.
- Final branch review approved.
- No open Critical or Important findings.
- Unit, integration, security, and E2E tests passing.
- Lint and build passing.
- Clean-wheel smoke passing.
- Authorized live OpenAI and Higgsfield smoke tests passing.
- Clean worktree.
- No credentials or absolute user paths in tracked files or exports.

Then call:

```json
{
  "status": "complete"
}
```

## Phase 6: Branch handoff

After verified goal completion, detect whether the workspace is a normal checkout, owned worktree, or harness-managed worktree.

Present exactly:

```text
Implementation complete. What would you like to do?

1. Merge back to main locally
2. Push and create a pull request
3. Keep the branch as-is
4. Discard this work
```

### Option 1: Merge locally

```bash
git switch main
git pull --ff-only
git merge feature/ai-sprite-studio-v1
uv run pytest -q
```

Only after the merged verification passes:

```bash
git worktree remove .worktrees/ai-sprite-studio-v1
git worktree prune
git branch -d feature/ai-sprite-studio-v1
```

Do not remove a harness-owned worktree.

### Option 2: Push and create a pull request

```bash
git push -u origin feature/ai-sprite-studio-v1
gh pr create --fill
```

Keep the worktree for review revisions.

### Option 3: Keep branch

Report:

```text
Keeping branch feature/ai-sprite-studio-v1.
Worktree preserved at .worktrees/ai-sprite-studio-v1.
```

### Option 4: Discard

First require the exact response:

```text
discard
```

Then show the branch commits and worktree path being removed. Only after confirmation:

```bash
git worktree remove .worktrees/ai-sprite-studio-v1
git worktree prune
git branch -D feature/ai-sprite-studio-v1
```

## Non-goals

- No custom orchestration application.
- No YAML workflow engine.
- No multi-agent concurrent editing.
- No automatic provider spending.
- No automatic merge, push, cleanup, or destructive rollback.
- No progress stored solely in conversation context.
