# Phase 8 design: recovery

Scope: PHASES.md Phase 8; MASTER_SPEC §6, §64–68; ARCHITECTURE §6.2, §14; DATA_MODEL §5. Principle: PostgreSQL is the truth; containers, Redis, and in-memory state are rebuilt from it; dead workers are never resurrected; every recovery decision is an event.

## Already in place (Phases 2–7)

Durable `operation_intents` for execution creation and merges (merges are re-sent while `MERGING`; Git Service is idempotent per approval); LOST detection for vanished containers; leases with epochs, renewal, failover, and adoption of orphaned tasks; single scheduler through a PostgreSQL advisory lock; notifications outbox; pause stops new work; Redis only for wake-ups (the scheduler polls without it).

## What Phase 8 adds

1. **Checkpoints** — `task_checkpoints(task_id, seq, reason, epoch, state, snapshot jsonb, created_at)`; snapshot = task state and resume state, requirement and plan versions, subtask states with workspace heads, lease provider/epoch, orchestrator cursor, budget consumed/reserved, open approvals. Written after each accepted step, before pause/cancel/failover/failback/revision/merge, and after each execution result. A task is at a **safe checkpoint** when no ORCHESTRATOR step is in flight, no fenced launch of the current epoch is queued in `pending_launches`, and its latest checkpoint is newer than its last state change.
2. **Recovery Controller** (`control_plane/recovery.py`), run once at startup before the scheduler loop and then every N ticks: (a) executions `REQUESTED/STARTING/RUNNING/STOPPING` vs Agent Manager `managed()` → re-dispatch REQUESTED, collect exited, mark absent `LOST`; (b) containers, networks, and volumes labelled for executions unknown to the database → removed; for terminal executions only after their output was collected (`result_artifact_ids` stored or the finalize path ran), never before; (c) intents `PENDING/SENT` older than a grace period → confirmed from the callee's state or re-sent once, then `FAILED` with an event; (d) `ACTIVE` workspaces whose clone is missing → `REMOVED` and their subtask `FIX_REQUIRED`; (e) verifications `PREPARING` without runners → relaunched; (f) expired leases → adopted (existing); (g) wake the scheduler (Redis rebuilt from PostgreSQL). Writes a `RECOVERY_COMPLETED` event with counts; `GET /v1/recovery` and `ho recovery status`.
3. **Failback** — when the lease provider is `codex`, Claude's credential is READY with no recent QUOTA/AUTH, and the task is at a safe checkpoint, acquire for Claude (new epoch, `ORCHESTRATOR_FAILBACK`); configurable (`orchestration.failback`, default on).
4. **Self-healing and DEGRADED** — a health monitor tracks consecutive failures of Agent Manager, Git Service, and Redis; after N failures the platform is `DEGRADED` (attention event + `/health` reports it), new dispatches back off exponentially instead of failing tasks, and recovery to healthy emits an event. A task whose recovery fails repeatedly (bounded retries with backoff) goes `BLOCKED` with an attention notification.
5. **Graceful pause/cancel with retention** — pause: no new launches, running executions finish (existing), checkpoint. Cancel: stop running executions with a grace period, collect workspace commits, retain workspaces (`RETAINED`), release test environments, final manifest. Resume continues from the latest checkpoint (pending launches kept, a step requested).
6. **Hermes outage** — an outbox deliverer (`HO_HERMES_WEBHOOK_URL`, optional until Phase 9) with exponential backoff and a cap; delivery state per notification; when unset or failing, notifications stay `PENDING` and work that needs no new approval continues; approval requests still wait in `APPROVAL_REQUIRED`.

## Tests

Unit: checkpoint snapshot builder, safe-checkpoint predicate, backoff schedule, health state machine. Integration (fake Agent Manager): restart with executions in each state; orphan containers; intents stuck in `SENT`; missing workspace; verification left `PREPARING`; failback only at a safe checkpoint and never during a step; DEGRADED on repeated Agent Manager failures and recovery; cancel retaining work; outbox delivery with a failing then recovering webhook. Smoke (`smoke-phase8.sh`): kill the control plane and Agent Manager mid-execution (`docker kill`), restart the stack, verify LOST/re-dispatch, no duplicate leaders or executions, intents reconciled; `docker compose restart` of the whole stack (reboot simulation); Redis flushed and stopped.

## Changes from the partial Codex adversarial review (2026-10-01; Codex hit its usage limit mid-review)

- Cleanup never removes the container of an execution whose output has not been collected; terminal-by-timeout or stopped executions are collected first.
- Failback and failover leave queued fenced launches of the old epoch in a defined state: a failover/failback waits for the safe checkpoint (no queued fenced launch), and launches the deterministic pipeline queued (unfenced) survive the epoch change.

## Risks to challenge

1. Re-dispatching a `REQUESTED` execution whose container was actually created (duplicate worker) — rely on Agent Manager idempotency by execution ID?
2. Removing containers of executions the database considers terminal while their result was not yet collected.
3. Failback flapping between providers when Claude's availability oscillates.
4. Recovery running concurrently with the scheduler on another instance (advisory lock scope).
5. Cancel collecting commits from a workspace an agent is still writing to.
