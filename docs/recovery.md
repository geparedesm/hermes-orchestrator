# Recovery

How hermes-orchestrator recovers from interruptions (Phase 8). Design: [docs/design/phase-8.md](design/phase-8.md); evidence: [docs/validation/phase-8.md](validation/phase-8.md).

## Principles

- PostgreSQL is the only truth. Containers, Redis, and in-memory state are rebuilt from it.
- Dead workers are never resurrected. Work continues in fresh workers from durable state.
- Recovery never crosses an approval boundary: it finishes or reconciles what was already authorized and leaves anything that needs a new decision waiting (`APPROVAL_REQUIRED`, `PAUSED_BUDGET`, `AUTH_REQUIRED`).
- Every recovery decision is an event, and every reconciliation is recorded in `recovery_runs`.

## What happens on start

Compose starts PostgreSQL and Redis, runs migrations, then the control plane. The scheduler leader (one per database, through a PostgreSQL advisory lock) runs a **startup reconciliation** before its first pass:

| Step | What it does |
| --- | --- |
| Executions | Each active execution is checked against Agent Manager: finished containers are collected; vanished ones become `LOST`; `REQUESTED` ones are left to the regular execution sync, which dispatches them again after a grace period, one dispatch at a time (no duplicate worker). |
| Orphans | Containers, networks, and volumes labelled for executions the database does not know, or knows as finished (their output was collected before they were marked finished), are removed. Live work is never touched. |
| Intents | Operation intents left `PENDING`/`SENT` by a crash are resolved from the state the call would have produced; merges still `MERGING` are re-sent (Git Service is idempotent per approval). |
| Workspaces | Active development workspaces whose clone vanished become `REMOVED`; their subtask is sent back so the orchestrator starts it again from a fresh clone. |
| Verifications | Verifications left `PREPARING` without runners are launched again. |
| Ended tasks | Done, cancelled, or failed tasks keep no running work, lease, or queued launch. |
| Retention | Workspaces of cancelled or failed tasks are collected (commits saved under platform refs) and kept as `RETAINED`. |
| Leases | Orchestrated tasks without a live lease are adopted with a new epoch (never reused). |

The same reconciliation runs every 12 scheduler passes (about a minute) and on demand: `ho recovery run`.

## Health and DEGRADED

Each scheduler pass probes Agent Manager, Git Service, and Redis. After three consecutive failures a component is `DEGRADED`: a `PLATFORM_DEGRADED` attention event is recorded, `/health/ready` and `ho recovery status` report it, and new executions wait in `REQUESTED` instead of failing their tasks. The first successful probe records `PLATFORM_RECOVERED` and dispatching resumes. Redis being down only removes instant wake-ups: the scheduler polls PostgreSQL.

## Orchestrator failover and failback

Claude is the preferred orchestrator. On Claude login, quota, or repeated step failures the lead moves to Codex at a step boundary with a new epoch (Phase 7). When Claude is available again, the lead returns to Claude (`ORCHESTRATOR_FAILBACK`) only at a **safe checkpoint** (no orchestrator step in flight, no fenced launch queued) and not within 10 minutes of the last failover. Set `orchestration.failback: false` in the platform configuration to keep Codex leading.

## Checkpoints

`task_checkpoints` keeps a snapshot of each task's durable state (state, requirement and plan versions, subtasks and workspace heads, lease, budget, open approvals, queued launches) at every state change, every applied orchestrator step, every execution result, and before failover and failback: `ho task checkpoints T-n`.

## Hermes outage

Notifications are written to the outbox in the same transaction as their event. The deliverer posts them to `HO_HERMES_WEBHOOK_URL` (bearer token from `HO_HERMES_WEBHOOK_TOKEN_FILE`) in order, backing off 10 s, 20 s, 40 s … up to 15 minutes while Hermes is unreachable, and delivers the backlog when it returns. Work that is already authorized continues meanwhile; decisions wait for a human.

## Pause, resume, cancel

- **Pause**: no new launches; running executions finish and their results are recorded, then decided again on resume.
- **Resume**: continues from durable state; queued launches are kept while a task waits.
- **Cancel**: running executions are stopped, open approvals invalidated, queued launches and the lease dropped, and the workspaces retained with their commits.

## Operator commands

```bash
docker compose exec -T control-plane ho recovery status      # health, open intents, pending notifications, recent runs
docker compose exec -T control-plane ho recovery run         # reconcile now
docker compose exec -T control-plane ho task checkpoints T-n
```
