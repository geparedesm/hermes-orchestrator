# Phase 7 Validation: Multi-Agent Orchestration

Branch `codex/phase-7-orchestration`. Design: [docs/design/phase-7.md](../design/phase-7.md) (revised after a Codex adversarial review). Orchestration is opt-in with `HO_ORCHESTRATION=true`; with it off (the default) the platform behaves as in Phase 6.

## What was built

- **Lease and steps** (`control_plane/orchestration.py`, migration `0006`): one leader per task in `task_leases`; acquiring bumps the epoch, renewing does not; the holder is a stable instance ID (`HO_INSTANCE_ID`). The orchestrator works in bounded ORCHESTRATOR executions (project read access, no workspace) that return a schema-validated `orchestrator-step` proposal. A step runs only when an external trigger event arrives after the task's event cursor, or when the previous step left nothing running (bounded: three in a row block the task).
- **Closed action vocabulary**: `SET_REQUIREMENTS`, `RECORD_ASSUMPTION`, `SET_PLAN`, `REQUEST_EXECUTION`, `ACCEPT_SUBTASK`, `REJECT_SUBTASK`, `REQUEST_APPROVAL`, `PROPOSE_KNOWLEDGE`, `SUBMIT_FOR_QUALITY_GATE`, `REPORT_BLOCKED`, `WAIT`. Each action is validated and applied in its own savepoint and recorded in `orchestrator_actions` with the outcome and reason; rejections are fed back to the next step. Actions from a stale epoch, or arriving while the task waits, are rejected.
- **Subtask DAG**: versioned plans, acyclic dependencies, readiness from accepted dependencies, revisions that keep accepted work and refuse to drop running subtasks, expansion beyond the profile only with a `SCOPE_EXPANSION` approval bound to that exact plan.
- **Deterministic cycle** (no model tokens): developer in the subtask's workspace (dependent subtasks start from their dependencies' accepted heads) → collect → review by the other provider of the subtask's own changes → fixes by the same developer up to `review.cycle_limit` → alternate developer in a fresh workspace or `BLOCKED`. Research subtasks completed without code are accepted without review. All subtasks accepted → integration → verification → integration review by every provider other than each developer (both providers when both developed) → Quality Gate → `READY_FOR_MERGE` (with a manifest) or `FIX_REQUIRED` with the failing requirements as orchestrator input.
- **Routing** (`ho_core/routing.py`): per-kind defaults, project preferences, orchestrator suggestion, availability (login, recent quota), load against the machine mix, and success history; every decision is recorded (`AGENT_ROUTED`).
- **Retries and fallback**: transient retries, alternate provider on quota, orchestrator failover to the other provider on AUTH/QUOTA or repeated failures (new epoch at a step boundary, no waiting for a login when the other provider can lead), adoption of tasks whose lease vanished.
- **Budgets** (`control_plane/budgets.py`): atomic reservations per launch (launch, retry, estimated provider usage), settled with reported usage, released when never started, charged in full when lost; review cycles and subtasks charged when they happen; thresholds; `PAUSED_BUDGET` (also during verification) and `BUDGET_INCREASE` approvals that resume the work.
- **Scheduling**: `pending_launches` for capacity, budget, scope conflicts (overlapping `files`), and higher-priority tasks, retried each tick by effective priority with aging from `launch_suspended_at`; launches of waiting tasks are kept until they resume.
- **Increment D**: duplicate/related classification before planning (`task_relationships`; a duplicate waits for the user), live revisions (`ho task revise`), HIGH or irreversible assumptions as `ASSUMPTION` approvals, project knowledge (`HYPOTHESIS` → confirmed/rejected by the operator, anchored to files, retrieved for developers, marked `STALE` when an accepted subtask changes its anchors), versioned requirement and plan artifacts, and Task Manifests built from records and validated against `manifest.schema.json` (`READY_FOR_MERGE`, `FINAL`, `ON_DEMAND`).
- **CLI and API**: `ho task revise|budget|inspect [--manifest]`, `ho knowledge list|confirm|reject`; `/v1/tasks/{key}/revise|budget|orchestration|manifest`, `/v1/projects/{slug}/knowledge`, `/v1/knowledge/{id}/decision`.

## Evidence

| Check | Result |
| --- | --- |
| `make lint` | pass |
| `make test-unit` | 217 passed (6 new: routing, plan cycles, similarity, step schema) |
| `make test-integration` | 104 passed (29 new in `test_orchestration.py`) |
| `scripts/smoke-phase7.sh` | all checks pass (throwaway identity with invalid logins; no billable requests) |
| `scripts/smoke-phase2.sh` … `smoke-phase6.sh` | all pass with orchestration off |

The integration suite covers the scenarios from the adversarial review: lease change during dispatch (fenced launch cancelled, stale-epoch actions rejected), WAIT without new events, concurrent reservations near a budget limit, mixed authorship (both integration reviews required), and aging that lets a suspended task outrank its preemptor.

### Real run with the operator's subscriptions (hello-api, T-9)

Request: add `farewell(name)` and `shout(text)` to the `hello` package with unittest tests. Final state `READY_FOR_MERGE` at integration commit `7dad7c5`; `main` untouched (the merge needs the operator's approval).

- Claude (orchestrator) wrote requirements and a three-subtask plan: `S1-research` → `S2-implement` → `S3-tests`.
- `S1-research`: Claude developed, Codex reviewed (approved). `S2-implement`: Codex developed, Claude requested changes (2 findings), Codex fixed, Claude approved. `S3-tests`: Codex developed, Claude approved (1 LOW).
- Integration of three workspaces (5 files, +128/-0), verification by the Test Runner passed, integration reviews by Codex (approved) and Claude (approved, 2 LOW: duplicated test coverage and a planning note committed at the repository root), Quality Gate PASS (risk LOW), ready-for-merge manifest generated.
- Executions: 11 orchestrator steps, 5 developer runs (3 Codex, 2 Claude), 6 reviews (4 Claude, 2 Codex), 1 test runner, all succeeded. Budget consumed: 23 launches (limit raised once through an approved `BUDGET_INCREASE`), 4 retries, 1 review cycle, 3 subtasks, 545,514 provider tokens.

### Defects found by the real runs (all fixed, each with a test)

The real runs on hello-api (T-8, then T-9) exposed problems the simulated agents did not:

1. Results arriving while a task was paused were applied → rejected and decided again on resume.
2. Accepting a subtask with work left did not trigger a step → orchestrator input with the ready subtasks.
3. The orchestrator requested a TESTER for a test-authoring subtask and the task stalled → test authoring runs as development; rejected actions with nothing running trigger a bounded re-step.
4. A dependent subtask started from the bare task base → it starts from its dependencies' accepted heads, and its review covers only its own changes.
5. The step context hid state changes, so a released duplicate looked blocked → the context shows the current state and state changes.
6. Lease holders were `hostname-pid`, so a recreated container waited for its own leases → stable `HO_INSTANCE_ID`; orphaned tasks are adopted and epochs are never reused.
7. A released task kept its plan but returned to PLANNING, and the context listed platform keys → planned tasks are queued on `REQUEST_EXECUTION`, and the context lists the orchestrator's own keys (platform-style keys are rejected in plans).
8. A research subtask without commits was sent back for fixes → accepted without code review.
9. An exhausted launch budget during verification left the task in TESTING → the task pauses for budget; an approved increase relaunches the verification.

Also fixed: AUTH continuations after a re-login lost their result schema, purpose, and subtask (a continued review would have used the wrong schema), and the AUTH flow now runs before the finish hooks.

Operator interventions during the real run, recorded as stand-ins: two `UPDATE tasks SET step_requested = true` to resume T-8/T-9 after deploying fixes for defects 2, 3, and 7; one manual removal of a stale lease for T-9 (defect 6); T-8 cancelled after defect 4 left it blocked; T-2 to T-6 (left over from earlier phases' validations) were picked up when orchestration was first enabled: T-5 and T-6 were paused, and T-2 to T-4 wait in `APPROVAL_REQUIRED` because the orchestrator recorded high-impact assumptions about their vague requests. The `BUDGET_INCREASE` for T-9 was approved on the operator's instruction.

## Codex review (`/codex:review --base main`)

Two findings, both fixed: automatic retries, fallbacks, alternate developers, and step retries were not charged to the retries budget; an approved scope expansion did not authorize the plan it was requested for (approvals are now bound to the plan digest and consumed on resubmission).

## Decisions made in this phase

- The model plans, the control plane drives: cross-review, fix cycles, integration, verification, the gate, budgets, and fallback are deterministic code.
- Integration-level review stays mandatory on the exact integration SHA; with both providers as developers, both review it (design change 4).
- An alternate developer starts a fresh workspace; earlier attempts are retained, not integrated (design change 5).
- Pausing a task stops new work but not running executions (Phase 2 semantics); their results are recorded and decided again on resume.
- Plan revisions cancel dropped subtasks instead of a separate `SUPERSEDED` state.

## Known limitations

- Single control-plane instance in practice; leases support more, but multi-instance operation is not exercised (Phase 8 recovery and Phase 11 hardening).
- Request similarity for deduplication is word overlap, not semantic.
- Knowledge staleness uses each subtask's declared `files`, not the actual diff.
- Manifests do not yet record worker image digests, agent versions, or command records (empty arrays, schema-valid).
- The provider-usage reservation is a fixed estimate (200,000 tokens per agent execution).
- Steps are prompted with a bounded context (about 20,000 characters); very large plans are summarized by truncation.

## Operator review

- [ ] Review the T-9 change in `~/HermesProjects/hello-api` (`ho task inspect T-9 --manifest`, `ho review show T-9`) and decide on its merge.
- [ ] Decide on T-2 to T-4 (pending assumption approvals) and T-5/T-6 (paused), left over from earlier validations.
- [ ] Approve Phase 7.
