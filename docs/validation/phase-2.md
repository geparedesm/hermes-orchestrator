# Phase 2 Validation: Minimal Control Plane

**Date:** 2026-09-30  
**Machine:** MacBook Pro (Apple Silicon), Docker Desktop 29.4.0, `linux/arm64`, Docker VM with 12 CPUs and 8 GB RAM  
**Status:** Approved by Gabriel Paredes on 2026-09-30 (PR #3)  
**Scope:** PHASES.md Phase 2 and its completion criteria: *registered projects and tasks persist across service restarts; scheduling and protected actions respect policy and approval checks.*

## What was built

| Deliverable | Location |
| --- | --- |
| Shared contracts: enums, task state machine, configuration layering and hard-policy clamp, Policy Engine (commands, actions, environments, capability grants), read-only environment detection, path confinement, UUIDv7, JSON logging | `packages/ho_core/` |
| PostgreSQL migration `0001` with a least-privilege `ho_app` role (audit tables append-only) | `migrations/`, `scripts/postgres-init.sh` |
| Task API, Project Registry and onboarding, Approval Service, basic Scheduler, idempotency, event log and notification outbox, Redis coordination, artifact store, operator CLI `ho` | `services/control-plane/` |
| Git Service, read-only: repository inspection and onboarding scan with hardened Git | `services/git-service/` |
| Compose stack with internal networks, Docker secrets, non-root read-only containers, a one-shot `migrate` service | `compose.yaml`, `scripts/init-secrets.sh`, `.env.example` |
| Machine profiles and platform configuration schema | `config/`, `schemas/platform.schema.json` |

## Evidence

| Check | Command | Result |
| --- | --- | --- |
| Unit tests (state machine, configuration and clamp, policy and grants, detection, paths) | `make test-unit` | 76 passed |
| Integration tests against PostgreSQL 17.6 and Redis 8.2 | `make test-integration` | 23 passed |
| End-to-end smoke test on a real Compose stack | `make smoke` | 21 of 21 checks passed |
| Schemas, examples, and machine profiles | `make validate-schemas` | 5 schemas valid, 4 examples accepted, 7 invalid examples rejected, 2 profiles valid |
| Static checks | `make lint` | clean |

The smoke test covers: registration confined to the projects root; onboarding proposal with hard-policy clamps; a task waiting in `BACKLOG` until the `PROJECT_READY` approval is consumed; scheduler promotion to `READY`; persistence across a `control-plane` restart and a full `docker compose down`/`up`; Redis stopped (readiness `degraded`, commands still work) and restored; command classification; no Internet route from the control plane; read-only project mount in git-service; non-root services; and the recorded state history.

Integration tests additionally prove: plugin requests without a forwarded human principal are rejected and the plugin cannot claim `host-cli`; non-approvers get `403`; an approval is invalidated when the repository head changes before use; approvals expire; `UNLIMITED` budgets require approval and fall back when rejected; dependencies hold tasks in `BACKLOG`; priority aging reorders the queue; unregistering refuses unfinished tasks and never deletes files; `ho_app` cannot update or delete audit events; repository hooks and `core.fsmonitor` never execute during a scan.

## OI-07: Hermes image

The pinned index digest `sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7` was pulled and run with `--network none` and a named volume at `HERMES_HOME=/opt/data`:

- `linux/arm64`: `hermes --version` → `Hermes Agent v0.21.5 (2026.9.24) · upstream f97608f1`.
- `linux/amd64` (emulated): same output.
- OCI label `org.opencontainers.image.revision` = `f97608f178d1ffeca59860195ab7da295f7c8e5f`, matching the source inspected in Phase 0.
- The image starts as root and uses s6-overlay to supervise its services; the data directory is owned by the `hermes` user.

Not yet done: running Hermes with the orchestration plugin (the plugin is built in Phase 9) and adding the `hermes` service to `compose.yaml`.

## Implementation decisions made in this phase

| Decision | Reason |
| --- | --- |
| The operator CLI `ho` runs inside the control-plane container (`docker compose exec control-plane ho ...`) and acts as `host-cli:operator` | No API port is published on the host (NETWORK_MODEL §7). |
| The projects root is mounted **read-only** into git-service | Phase 2 only reads; write access arrives with Phase 5 operations. |
| `HO_PROJECTS_ROOT_HOST` in `.env` is the single source for the projects root; it overrides `platform.projects_root_host` | The Compose bind mount and the control plane must agree. |
| Optional untracked `config/local.yaml` | Machine-specific platform overrides without editing tracked profiles. |
| `FAILED` and `CANCELLED` stay terminal; retry creates a new task linked with `RELATED` | Keeps history immutable; DATA_MODEL §4.1 updated. |
| `MERGING → BLOCKED` added | A failed or unconfirmable merge needs human attention; DATA_MODEL §4.1 updated. |
| A task requesting `UNLIMITED` runs under the project's budget profile until the approval is consumed | The database forbids `UNLIMITED` without an approval ID. |
| The Phase 2 dispatcher (`NoWorkersDispatcher`) never dispatches | There is no Agent Manager yet; tasks wait in `READY` rather than pretending to run. |

## Known limitations

- Linux was validated only through the configuration schema; the stack has not yet been run on a Linux host.
- Pause is immediate because no executions exist yet; graceful pause and cancel with checkpoints are Phase 8.
- The notification outbox is written but not delivered; Hermes delivery is Phase 9.
- Onboarding proposals are rule-based. Claude-generated proposals arrive with the orchestrator (Phases 4 and 7).
- The mac profile's three NORMAL workers need 12 GB plus services, more than this Docker VM's 8 GB. Raise Docker Desktop's memory or lower the profile before Phase 3.
- Tests use FastAPI's `TestClient`, which currently emits a Starlette deprecation warning about `httpx`; it does not affect the services.
