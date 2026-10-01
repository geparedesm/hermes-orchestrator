# Phase 7 design: multi-agent orchestration

Scope: PHASES.md Phase 7. Builds on AD-04 (orchestrator = bounded steps returning a validated action proposal) and AD-08 (leases and epochs in PostgreSQL).

## Principle: the model plans, the control plane drives

The orchestrator (Claude, Codex as fallback) only decides what cannot be decided mechanically: requirements, assumptions, the DAG, and how to react to failures. Everything the specification makes mandatory runs as deterministic control-plane code, so it cannot be skipped and costs no model tokens: cross-review by the other provider, fix → retest → re-review cycles and their limits, integration, verification, the Quality Gate, budgets, retries, and provider fallback.

## Increment A — lease, step loop, actions, DAG, task flow

- `task_leases(task_id, holder, provider, epoch, expires_at, renewed_at)`. Acquire: `UPDATE … SET epoch = epoch + 1, holder = $me … WHERE expires_at < now() OR holder = $me`. Every grant already carries `lease_epoch`; applying a step's actions checks `epoch` in the same transaction and rejects stale steps.
- `OrchestratorDispatcher` replaces `NoWorkersDispatcher`: takes READY tasks in queue order (priority + aging, existing), acquires the lease, READY → PLANNING, and requests a step.
- A step is an ORCHESTRATOR execution (grant: project_read of the main checkout, no workspace, PROVIDER_ONLY egress), prompt = context bundle (request, latest requirements, assumptions, DAG with subtask states, events since the last step, review findings, test and gate results, budget state, relevant knowledge), result schema `orchestrator-step` (summary + list of actions, closed vocabulary).
- Steps run only when something relevant happened (`tasks.step_requested_at`, set by events: execution finished, review recorded, verification done, approval decided, requirement revised) and never twice concurrently per task (unique active ORCHESTRATOR execution).
- Actions: `SET_REQUIREMENTS`, `RECORD_ASSUMPTION`, `SET_PLAN`, `REQUEST_EXECUTION` (DEVELOPER/TESTER for a subtask), `ACCEPT_SUBTASK`/`REJECT_SUBTASK`, `REQUEST_APPROVAL`, `PROPOSE_KNOWLEDGE`, `SUBMIT_FOR_QUALITY_GATE`, `REPORT_BLOCKED`, `WAIT`. Each is validated (schema, epoch, Policy Engine, state machine, budgets) and stored in `orchestrator_actions` (accepted/rejected + reason) for the audit trail. Rejections are fed back in the next context bundle.
- New tables: `requirement_versions`, `assumptions`, `subtasks` (key `T-n-m`, kind, title, spec artifact, state, developer/reviewer provider, estimated_scope, risk, resource profile, review_cycles, attempts, workspace_id), `subtask_dependencies` (cycle check on insert), `orchestrator_actions`.
- Task flow: PLANNING → QUEUED (plan accepted) → RUNNING (subtasks executing) → TESTING (integration verification) → REVIEW (integration evidence check) → QUALITY_GATE → READY_FOR_MERGE / FIX_REQUIRED. FIX_REQUIRED → RUNNING when the orchestrator plans fixes.

## Increment B — subtask cycle, router, cross-review, retries

- Subtask cycle (deterministic): developer execution in the subtask's workspace → collect → automatic REVIEWER execution by the other provider on the subtask head (review-result schema) → APPROVED: subtask ACCEPTED; CHANGES_REQUESTED: fix execution by the original developer on the same workspace with the structured findings → re-review. `review.cycle_limit` (default 2) then `review.on_limit`: alternate developer (other provider, same workspace) or BLOCKED.
- Router: per subtask, score each available provider: kind preference (planning/architecture/debugging/research → Claude; implementation/tests/refactor → Codex; project `agents.preferred` overrides), availability (credential READY, no recent QUOTA/AUTH), load vs `starting_mix` (1 Claude + 2 Codex on Mac), historical success rate and mean duration per provider and kind (from `executions`), budget state. The orchestrator's preferred provider is a strong but not absolute input; the decision and its factors are recorded.
- Retries: TRANSIENT → retry (`retries.transient`); AUTH/QUOTA → provider unavailable, same subtask to the alternate provider (`retries.alternate_developer`); TASK → fix attempts (`retries.fix_attempts`) then alternate developer, then BLOCKED. All count against the retries budget.
- Orchestrator fallback: if Claude fails AUTH/QUOTA or N consecutive steps, the lease provider switches to Codex at the next step (checkpoint), and back when Claude is READY.
- Cross-review evidence for the gate: every integrated subtask needs an APPROVED review by a provider other than its developer on its final head; the integration commit adds only merges of reviewed heads (plus reviewed conflict resolutions). The Phase 6 integration-level review remains for tasks without subtasks.

## Increment C — integration, verification, gate, budgets, scheduling

- All subtasks ACCEPTED → integrate (existing) → verification (existing) → gate (existing, extended with subtask-level review evidence) → READY_FOR_MERGE (+ push and PR for GitHub projects) or FIX_REQUIRED with the failing requirements in the next context bundle.
- Budgets: runtime (since task start), launches (existing), retries, review cycles, subtasks, provider usage units (tokens from `usage_records`). 70% warning event, 85% OPTIMIZE (told to the orchestrator), 100% PAUSED_BUDGET. `ho task budget T --add launches=N…` creates a BUDGET_INCREASE approval; approval raises the limits and resumes.
- Expansion: new subtasks beyond the first plan are allowed within the subtask budget and original scope; a plan that adds more than the expansion profile allows, or expands scope (new top-level areas), needs SCOPE_EXPANSION approval.
- Conflict-aware scheduling: subtasks with overlapping estimated scope inside a task are serialized; across tasks of a project, a READY task whose plan overlaps an active task's scope waits (relationship CONFLICTING) unless it has higher priority, in which case the lower task stops receiving new launches (soft preemption, never killing executions).

## Increment D — relationships, revisions, assumptions, memory, manifests

- Dedup on task creation: request similarity and project vs active tasks → DUPLICATE (task goes BLOCKED with the reason, user releases or cancels), RELATED (linked, shares context, not permissions), INDEPENDENT. CONFLICTING is decided at planning from scopes.
- Live revision: `ho task revise T "…"` → new requirement version → orchestrator step with impact analysis (KEEP/REPLAN/CANCEL/NEW per subtask); cancelled subtasks stop; major changes need approval.
- Assumptions: LOW/MEDIUM recorded; HIGH → ASSUMPTION approval and APPROVAL_REQUIRED.
- Knowledge: `knowledge_items` (category, trust, provenance, anchors, observed_at_commit); `PROPOSE_KNOWLEDGE` stores HYPOTHESIS/OBSERVED; retrieval by anchors overlapping the subtask scope; items whose anchors change become STALE; `ho knowledge confirm|reject`.
- Context artifacts per step (requirements.md, plan.json, assumptions, review feedback, changed files) and Task Manifests (READY_FOR_MERGE, FINAL, on demand: `ho task inspect`) built from records, sanitized, validated against `manifest.schema.json`.

## Risks to challenge

1. Two control-plane instances or a slow step applying actions after the lease moved (epoch check is the defence).
2. A model proposing actions that bypass budgets, approvals, or cross-review (closed vocabulary + deterministic cycle).
3. Event storms re-triggering steps and burning tokens (step debounce + one active step per task + budget).
4. Subtasks with mixed providers: is subtask-level review evidence sufficient for the gate, or must the integration be reviewed again?
5. Soft preemption never freeing capacity if lower-priority executions are long.

## Changes after the Codex adversarial review (2026-10-01)

Codex (verdict: needs attention) found six problems; all are accepted and change the design:

| # | Finding | Change |
|---|---|---|
| 1 | Epoch check at apply time does not fence side effects: `Executions.request` passes no epoch (grants default to 1), dispatch runs after commit, and `holder = $me` re-acquisition bumps the epoch under a running step. | Acquire (bumps epoch) and renew (does not) are separate operations. Executions, intents, and Git operations requested by orchestration carry the task's current epoch; the dispatcher and every after-commit call re-read the lease under lock and abandon work whose epoch is stale. Agent Manager and Git Service are fenced by being callable only by the control plane, which performs this check; results of executions started under an older epoch are still ingested (they are facts), only decisions are fenced. |
| 2 | A step's own completion (WORKER_STOPPED) requests the next step: WAIT loops forever. | Each task keeps an event cursor (`orchestrator_cursor_seq`). A step is requested only by new events of a fixed trigger set from outside the orchestrator (subtask execution finished, review recorded, verification finished, gate evaluated, approval decided, requirement revised, budget raised). An orchestrator failure retries at most twice. |
| 3 | Token usage is recorded after the fact, so concurrent launches overshoot the provider budget; lost executions are never charged. | Each launch reserves budget atomically under the budget row lock (launches, retries, and an estimated provider-usage charge per execution); finalize replaces the reservation with actual usage; LOST or unparseable executions keep the full reservation. Executions are bounded per run (turn limit, grant timeout capped by the remaining runtime budget). At 100% no new launches start; running executions finish within their reservation, then the task pauses. |
| 4 | Per-subtask reviews do not cover interactions in the integrated result. | The gate keeps requiring review of the exact integration SHA (requirements and interactions). With one developer provider: one integration review by the other provider. With both providers as developers: one integration review by each provider. Subtask reviews remain as early feedback and evidence. |
| 5 | Switching to the alternate developer on the same workspace lets a provider approve code it wrote. | Authorship is recorded per execution. An alternate developer starts a fresh attempt in a new workspace from the task base; the earlier attempt's commits are not carried over. A subtask whose final head has authors from both providers cannot be accepted without an explicit approval. |
| 6 | Soft preemption can starve a suspended task forever (aging applies only to READY). | Suspension is persisted (`launch_suspended_at`); suspended tasks take part in launch selection with aging, so their effective priority rises until they outrank the preemptor; a test proves resumption within the aging bound. |

New validation scenarios from the review: lease change during dispatch; WAIT without external events; concurrent launches near the budget limit; mixed authorship; starvation under repeated preemption.
