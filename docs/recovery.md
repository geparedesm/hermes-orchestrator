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
| Intents | Operation intents left `PENDING`/`SENT` by a crash are resolved from the state the call would have produced; merges still `MERGING` are re-sent (Git Service is idempotent per approval). For GitHub merges, a durable Git ref binds the attempted operation to the approval before calling GitHub; a retry can reconcile a merged PR only with that matching intent. Verification and retries always use the PR’s exact merge commit, even if the target branch has advanced. |
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

Notifications are written to the outbox in the same transaction as their event. The deliverer posts them to `HO_HERMES_WEBHOOK_URL` (signed with Hermes HMAC V2 using `HO_HERMES_WEBHOOK_SECRET_FILE`) in order, attention events one by one and routine ones in a digest every 5 minutes, backing off 10 s, 20 s, 40 s … up to 15 minutes while Hermes is unreachable, and delivers the backlog when it returns. Work that is already authorized continues meanwhile; decisions wait for a human.

## Pause, resume, cancel

- **Pause**: no new launches; running executions finish and their results are recorded, then decided again on resume.
- **Resume**: continues from durable state; queued launches are kept while a task waits.
- **Cancel**: running executions are stopped, open approvals invalidated, queued launches and the lease dropped, and the workspaces retained with their commits.

## Operator commands

Integration conflict workspaces start from the partial integration, including previously accepted work, and receive
the project's test services. A resolver reporting `blocked` or `failed`, or an unchanged conflict after its resolution
attempt, leaves the task `BLOCKED` with a reason instead of repeatedly resubmitting the same integration. After fixing
the cause, the operator can explicitly request `ho git resolve T-n --provider codex` on an active task; successful
resolution resumes integration, verification, cross-review, and the normal merge approval.

```bash
docker compose exec -T control-plane ho recovery status      # health, open intents, pending notifications, recent runs
docker compose exec -T control-plane ho recovery run         # reconcile now
docker compose exec -T control-plane ho task checkpoints T-n
```


## Missing pinned worker images

`make images` now atomically saves a deduplicated, gzip-compressed local recovery archive at
`config/image-cache/execution-images.tar` before publishing the image lock. Docker detects the compression automatically. Keep enough host disk
space for both Docker images and this archive (and a temporary replacement during rebuilding).
The archive contains built images, never provider credential volumes, and is excluded from Git
and Docker build contexts. Critical-state backups exclude this regenerable archive. Agent Manager reads it through its existing read-only config mount.

Before spending a launch, Control Plane asks Agent Manager to verify the exact pinned image ID.
Missing allowed images trigger a serialized background Docker import; pending work
resumes automatically when its image is restored. No mutable tag, registry fallback, or unapproved
worker build is substituted for a pin. A failed archive is not repeatedly imported. An image absent from the archive does not prevent other images
from being recovered again after a later deletion. If the archive
is missing, corrupt, or does not contain the pinned ID, the waiting reason tells the operator to run
`make images`. Automatic recovery requires a successfully built local archive first.

If an image disappears between preflight and dispatch, the same execution waits up to 30 minutes
without spending dispatch retries. Expiry reports an infrastructure failure with the actual reason.
An orchestrator refused before startup blocks with that cause rather than cycling providers.
Launches definitively refused while still REQUESTED refund their launch charge and unused reservations.
Ambiguous Docker communication failures keep their launch charge because a remote start may have succeeded.
