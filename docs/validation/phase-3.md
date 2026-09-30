# Phase 3 Validation: Agent Manager

**Date:** 2026-09-30  
**Machine:** MacBook Pro (Apple Silicon), Docker Desktop 29.4.0, `linux/arm64`, Docker VM with 12 CPUs and 8 GB RAM  
**Scope:** PHASES.md Phase 3 and its completion criteria: *workers can be managed through the private authenticated API, and forbidden mounts, networks, and capabilities are denied.*

## What was built

| Deliverable | Location |
| --- | --- |
| Agent Manager: hard-invariant planning, pinned image allowlist, workers, per-execution egress networks and proxies, task service networks, capacity checks, output collection, cleanup, expired-grant reaper | `services/agent-manager/` |
| Egress proxy: CONNECT-only on port 443, domain allowlists, post-resolution IP checks, JSON decision log | `services/egress-proxy/` |
| `agent-base` execution image (non-root, `tini`, Git, curl) | `workers/agent-base/` |
| Image build and pinning | `scripts/build-images.sh` → `config/images.lock.yaml` (machine-specific, untracked) |
| Control plane: execution requests with Policy Engine grants, concurrency and budget checks, durable intents, dispatch and retry, reconciliation (finish, lost, timeout), output artifacts with redaction, cancel and replace | `services/control-plane/.../executions.py`, migration `0002` |
| Operator CLI: `ho execution run|list|show|stop|replace`, `ho workers` | `services/control-plane/.../cli.py` |
| Compose: `agent-manager` service (non-root, socket group only, read-only projects root) | `compose.yaml` |

## Evidence

| Check | Command | Result |
| --- | --- | --- |
| Unit tests (adds egress policy) | `make test-unit` | 107 passed |
| Integration tests (adds 14 execution tests with an in-memory Agent Manager) | `make test-integration` | 37 passed |
| Agent Manager against the real Docker daemon | `make test-docker` | 25 passed |
| Phase 3 end-to-end on the Compose stack | `make smoke-phase3` | 21 of 21 checks passed |
| Phase 2 end-to-end (regression) | `make smoke` | 21 of 21 checks passed |
| Lint, schemas, profiles | `make lint validate-schemas` | clean |

### Forbidden operations that were shown to fail (MASTER_SPEC section 89)

Against real containers (`tests/docker`, `scripts/smoke-phase3.sh`):

- **Docker socket:** absent in workers; any request that adds it (raw `mounts`, `privileged`, `network_mode: host`, `DOCKER_HOST`) is ignored or rejected; grants with `docker` other than `NONE` are rejected.
- **Other projects and host paths:** workspaces outside `<project>/.hermes/worktrees/<name>`, another project's worktree, the user's main checkout, `..` traversal, symbolic links, and `/var/run/docker.sock` as a workspace are all rejected; a worker cannot read its project's files outside the workspace.
- **Platform services (N01):** workers cannot reach PostgreSQL or the control plane by IP, and the proxy refuses their service names.
- **Network:** runners without egress have no route; agent workers have no direct route (only the proxy); non-allowlisted domains, `host.docker.internal`, `169.254.169.254`, IP literals, and plain HTTP are refused by the proxy; external DNS does not resolve inside workers (N08); a task cannot reach another task's service network (N06).
- **Limits:** the machine's agent-worker cap and the Docker VM's memory are enforced by Agent Manager, and the control plane enforces the machine and project caps and the task's agent-launch budget (exhaustion moves the task to `PAUSED_BUDGET`).
- **Grants:** expired, mismatched, secret-bearing, and unpinned-image requests are rejected; the grant stored and sent is the intersection of request, role, project, and hard policy (a `PROD_WRITE` request becomes `NONE`); grants are revoked when executions end; the reaper stops workers whose grant expired.
- **Hardening verified on real containers:** UID 10001, read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, not privileged, memory, CPU, and PID limits from the resource profile.

### Lifecycle behaviors

- Output files and logs are collected, redacted, and stored as artifacts; the egress decision log is stored as `egress.jsonl`.
- Cancelling a task stops its executions; no platform containers remain for the task afterwards.
- Restarting `agent-manager` during a running execution does not lose it (state is in Docker labels).
- If Agent Manager is unreachable, the execution stays `REQUESTED` and is dispatched when it returns; a vanished container becomes `LOST`; a missing provider credential fails as `AUTH` and emits `AUTH_REQUIRED`.
- Replacing an execution stops it and starts a new one with a freshly evaluated grant, reusing its worker slot.

## Decisions made in this phase

| Decision | Reason |
| --- | --- |
| One egress proxy and internal network per agent execution (NETWORK_MODEL §4 updated) | Executions of one task can hold different egress grants; a shared per-task proxy could enforce only one. |
| Purpose-built CONNECT-only proxy instead of Squid (OI-05) | Small, reviewable, exactly the required rules, no plain HTTP. |
| Workers use `127.0.0.1` for DNS | Closes the DNS exfiltration channel measured by N08 while keeping names on internal networks. |
| Images pinned by local image ID in an untracked `config/images.lock.yaml` | Locally built images have no registry digest; the lock is regenerated by `make images`. |
| Agent Manager runs as UID 10003 with the Docker socket's group (`HO_DOCKER_GID`) | Least privilege: socket access without running as root. |
| Workspace directories must already exist | Git Service creates them from Phase 5; Agent Manager never lets Docker create host paths. |
| Only the operator identity may start or replace executions in this phase | The orchestrator requests executions through action proposals from Phase 4/7; the plugin should not start workers directly. |
| Secrets in grants are rejected | The Secrets Broker is Phase 4 scope; failing explicitly is safer than a partial implementation. |
| Provider credential volumes are mounted at `/home/agent/.ho-credentials/<provider>` | Provisional; Phase 4 sets the path each CLI officially uses. |

## Known limitations

- `provider_domains` in the machine profile are empty until Phase 4 records them from official provider documentation, so `PROVIDER_ONLY` currently denies everything.
- The implement → test → fix → retest → commit cycle inside one worker needs the Phase 4 adapters; in this phase an execution runs one command.
- Graceful stop is SIGTERM followed by SIGKILL after a grace period; checkpoint-aware pause is Phase 8.
- Ephemeral test services and project Compose environments are Phase 6 (the per-task service network already exists).
- Docker Desktop still has 8 GB: Agent Manager refuses executions that do not fit, so three NORMAL (4 GB) workers cannot run at once until memory is raised.
- The TOCTOU window between validating a workspace path and Docker mounting it is closed only because workers cannot write the `worktrees` directory itself; Phase 5 (Git Service owns worktree creation) keeps it that way.
- Linux has not been run yet.
