# Phase 8 Validation: Recovery

Branch `codex/phase-8-recovery`. Design: [docs/design/phase-8.md](../design/phase-8.md). Procedures: [docs/recovery.md](../recovery.md).

## What was built

- **Checkpoints** (`task_checkpoints`, migration `0007`): a snapshot of each task's durable state at every state change, every applied orchestrator step, every execution result, and before failover and failback (`ho task checkpoints`).
- **Recovery Controller** (`control_plane/recovery.py`): run by the scheduler leader before its first pass, every 12 passes, and on demand (`ho recovery run`). It reconciles active executions with Agent Manager (collect, `LOST`, re-dispatch), removes resources of unknown or finished executions (containers, networks, volumes), resolves stale intents, marks vanished workspaces and sends their subtasks back, relaunches verifications never launched, winds down ended tasks (no running work, lease, or queued launch), retains cancelled work after collecting its commits, and adopts orchestrated tasks without a lease. Each run is stored in `recovery_runs` and reported by a `RECOVERY_COMPLETED` event.
- **Health and DEGRADED** (`component_health`): Agent Manager, Git Service, and Redis are probed every pass; three consecutive failures make a component `DEGRADED` (attention event, `/health/ready`, `ho recovery status`), and every dispatch path waits instead of spending attempts; the first good probe records the recovery.
- **Outbox delivery**: notifications are posted to `HO_HERMES_WEBHOOK_URL` in order with a 10 s to 15 min backoff gated by the oldest pending one; unset or unreachable Hermes leaves them pending while authorized work continues (Phase 9 provides the receiver).
- **Failback**: the lead returns to Claude only at a safe checkpoint (no step in flight, no fenced launch queued) and not within 10 minutes of the last failover (`orchestration.failback`).
- **Wind-down on any terminal state**: cancelled, failed, or done tasks stop their executions and drop queued launches and leases; cancel already stopped executions (Phase 3).

## Evidence

| Check | Result |
| --- | --- |
| `make lint` | pass |
| `make test-unit` | 217 passed |
| `make test-integration` | 123 passed (19 new in `test_recovery.py`) |
| `scripts/smoke-phase8.sh` | 19 checks pass on a real Compose stack |
| `scripts/smoke-phase2.sh` … `smoke-phase7.sh` | all pass (the Phase 4 smoke found a recovery defect, below) |

The Phase 8 smoke injects real failures with Docker: the control plane killed mid-execution (the worker survives, exactly one worker, a second startup reconciliation); a worker removed while the control plane was down (reconciled as `LOST`); the whole stack restarted (tasks and checkpoints kept, new work runs); Agent Manager stopped (`DEGRADED`, a new execution waits in `REQUESTED`, then runs after `PLATFORM_RECOVERED`); Redis stopped (a task is still scheduled from PostgreSQL); a task cancelled with unfinished commits (workspace `RETAINED`, clone kept, no lease or queued launch left). Raw-command executions only: no provider login and no billable request.

## Codex reviews

- **Adversarial review of the design** (cut short by Codex's usage limit): two findings, both adopted: cleanup never removes an execution's resources before its output is collected, and failover/failback wait for a safe checkpoint so queued fenced launches of the old epoch are not lost.
- **Code review (`/codex:review --base main`)**: four findings, all fixed: recovery dispatch now honors the DEGRADED hold (the gate is in the shared dispatch path); workspaces are retained only after a successful collection (failed collections are retried); orphans are found through networks and volumes as well as containers; the outbox's oldest pending notification gates delivery, so order and backoff survive an outage.

## Defect found by the regression smoke tests

The Phase 4 smoke failed reproducibly after the first Phase 8 build: a periodic reconciliation dispatched an execution while the API was still creating its worker; Agent Manager's rollback of the second creation removed the first one's container, and the execution became `LOST`. Fixed by leaving dispatching to the execution sync (which waits five seconds before re-dispatching); the integration test now checks a single worker after recovery.

## Decisions made in this phase

- Recovery repairs only what was already authorized; anything that needs a decision stays waiting.
- The startup reconciliation runs inside the scheduler leader (advisory lock), so two control planes never reconcile at once.
- Only the execution sync dispatches, after a grace period: two concurrent creations of one worker undo each other in Agent Manager (its rollback removes the execution's resources), so reconciliation counts `REQUESTED` executions but never dispatches them.
- Finished executions' resources are removable because finalize collects output before marking an execution terminal.
- Redis keeps no state: recovery only sends a wake-up.

## Known limitations

- Multi-instance control planes are fenced (leases, epochs, advisory lock) but not exercised.
- Hermes delivery is tested against a mock receiver; the real receiver arrives in Phase 9.
- A failed orchestrator recovery is reported (`DEGRADED`, events) but not escalated through Hermes until Phase 9.
- Health probes run every scheduler pass; there is no separate watchdog process (Compose restarts crashed services).

## Operator review

- [ ] Review `ho recovery status` on the live stack after `make up`.
- [x] Merged under the operator's standing authorization (2026-10-01: "haz tu los merge cuando cumplas las fases").
