# Phase 11 Validation: Hardening

Branch `codex/phase-11-hardening`. Operations: [docs/operations.md](../operations.md). Security and failure test matrix: [docs/security-tests.md](../security-tests.md).

## What was built

- **Dependency caches** (MASTER_SPEC §71): per project and ecosystem (`ho-cache-<project>-pip|npm|gradle|pub|composer`) for executions that write a workspace, pointed to by each package manager's own cache variable; never shared between projects; trimmed least-recently-used beyond `machine.dependency_cache.max_gb_per_cache`, never while any managed container uses them (trimming holds Agent Manager's creation lock); `ho cache list|clear`.
- **Daily maintenance and retention** (§80): execution outputs removed by task outcome and project retention (metadata and digests kept; requests, requirements, plans, onboarding reports, manifests kept), retained workspaces removed after `failed_workspace_days`, delivered notifications and recovery reports pruned; `ho maintenance run`.
- **Backups and restore** (§79): `scripts/backup.sh` (PostgreSQL dump, Hermes state with consistent SQLite copies and secrets removed, configuration and the image allowlist, artifacts; checksums; pruning) and `scripts/restore.sh` (checksums verified, fresh database, Hermes state, artifacts, allowlist, startup reconciliation). Credentials, Redis, workers, and test environments are never included.
- **Approval-controlled updates and rollback** (§3, §79): `scripts/update.sh` requests an `UPDATE` approval bound to the running version and the target, refuses a different target, consumes the approval, snapshots, builds, starts, and runs `scripts/check.sh`; on failure `scripts/rollback.sh` returns to the previous images and the pre-update state; `platform_updates` records each run (migration `0008`).
- **Merge approval at READY_FOR_MERGE**: a passing Quality Gate requests the action-bound MERGE approval itself, so the person approves from Hermes (`/orch approve`), the Dashboard, or the CLI (§90).
- **Health check** `scripts/check.sh` / `make check`; **security suite** `make test-security`; **multi-architecture check** `scripts/check-multiarch.sh` (`HO_PLATFORM` in `build-images.sh`).
- **Documentation**: README covering every item of §93 for macOS and Linux; operations, recovery, Hermes, and security-test guides.

## Evidence

| Check | Result |
| --- | --- |
| `make lint`, `make validate-schemas` | pass |
| `make test-unit` | 240 passed |
| `make test-integration` | 146 passed |
| `make test-docker` | 48 passed (the suite runs as its own stack, `ho-test-docker`, so the operator's live stack never takes its workers for orphans) |
| `make test-security` | 62 passed (39 integration + 23 real-Docker) |
| `scripts/smoke-phase11.sh` | all checks pass |
| `scripts/smoke-phase2.sh` … `smoke-phase10.sh` | all pass (re-run after the stack-isolation change, next to the operator's live stack) |
| `scripts/check-multiarch.sh` (linux/amd64 on Apple Silicon) | all checks pass: services run as amd64, toolchains, Codex CLI, Claude Code binary is x86-64, browser runner, Hermes digest published for amd64 |
| Acceptance scenario (§90) with real Claude and Codex | see below |

### Failure injection (`scripts/smoke-phase11.sh`)

PostgreSQL stopped (readiness `unavailable`, then reconnection: new tasks accepted, scheduler resumed); Redis restarted (scheduling continues); dependency cache created, kept for the next execution, invalidated; maintenance; backup with checksums and no credential files, no secret values in Hermes's configuration, no service secret anywhere; restore (a task created after the backup is gone, earlier ones kept, Hermes plugin enabled); an update requested as an approval, refused before approval, refused for another version without consuming the approval, applied, recorded; a failing update rolled back automatically (previous version, state restored, `ROLLED_BACK` recorded, healthy).

### Architectures

All pinned base images are multi-architecture index digests (amd64 and arm64): Python, Debian, Node, Playwright, PostgreSQL, Redis, and Hermes. The macOS profile (`config/mac-m2-pro.yaml`) and the Linux profile (`config/linux.yaml`) are separate and selected by `HO_MACHINE_PROFILE`.

### Acceptance scenario (§90) with real Claude and Codex

On the operator's stack (upgraded in place to this phase: migrations `0007` and `0008` applied to its existing database, `make check` passing) with `HO_ORCHESTRATION=true`, a local project `acceptance-shop` (a small standard-library web app with unit tests, its own Compose service, and a browser check):

1. **Hermes → Task API**: the task was created through Hermes's tool registry (`orch_task_create`): "Add a /health endpoint that returns `{"status": "ok"}` … and show the number of products in the page heading … Add unit tests". T-10.
2. **Claude** (orchestrator) wrote requirements and a two-subtask plan (S1 → S2).
3. **Codex** implemented both subtasks in isolated workspaces; **Claude** cross-reviewed and accepted each (S1's first review was lost, see below, and was requested again automatically).
4. **Git Service** integrated both workspaces; verification ran with no test step (the repository's configuration was invalid, see defect 4 and T-11 below); **Claude** reviewed the integrated commit.
5. **Quality Gate**: all requirements passed except the policy-violation rule — the agents had run `which docker` / `docker --version` (rule CMD-H07, high-risk, advisory; workers have no Docker) — so the gate asked for an exception, which the operator approved with a note (`HIGH_RISK_OPERATION`).
6. **READY_FOR_MERGE**: the control plane requested the MERGE approval for the exact integrated commit; nothing merged.
7. The merge was **approved from Hermes's Dashboard** (`dashboard:operator`); Git Service merged into `main` (`865ca4f`), **post-merge verification passed**, the task became **DONE**, `TASK_COMPLETED` was emitted (waiting in the outbox: no chat channel configured), and READY_FOR_MERGE and FINAL manifests were generated.

The merged repository passes its tests and has the requested endpoint and heading.

T-10's verification ran no test step (the repository's `project.yaml` was invalid, see defect 4), so the scenario was run again as **T-11** ("add `GET /products/<id>`… with unit tests") after the fixes, with the corrected configuration (`python -m unittest -v`, browser tests enabled; the configuration change was re-approved as drift). The orchestrator first raised a high-impact assumption because it could not see the project (defect 5); after the fix it planned two subtasks from the real code, Codex implemented and Claude reviewed both, and the verification ran **build, the unit test suite, and the Browser Runner** against the project's Compose service — all `PASSED` — before the gate moved the task to `READY_FOR_MERGE` with a MERGE approval bound to the integrated commit (about eight minutes from approval to `READY_FOR_MERGE`). The operator approved the merge; Git Service merged it into `main` (`b9fe145`), **post-merge verification ran the tests again and passed**, the task became **DONE**, and `TASK_COMPLETED` and the manifests were generated.

### Defects found by the validation runs (all fixed, with tests)

1. **Two stacks on one Docker host destroyed each other's workers**: while the Phase 11 smoke ran next to the acceptance task, the smoke stack's orphan cleanup removed the acceptance task's reviewer (its execution was unknown to the smoke database). Every resource now carries `ho.stack` (the Compose project name); listing, reaping, and orphan cleanup see only their own stack, and task-scoped names (session volumes, task networks, test-service projects) are prefixed outside the main stack.
2. **A lost subtask review left the subtask waiting forever**: failed or lost reviews are now retried (at most twice, then handed back to the orchestrator), and a subtask in review with nothing running or queued gets its review requested again — which resumed the acceptance task after the fix was deployed.
3. **Cross-architecture builds of the Claude image failed**: QEMU aborts Claude Code's native x86-64 binary on Apple Silicon. Cross builds skip the image's CLI self-check (`CLI_SELF_CHECK=0`) and `check-multiarch.sh` verifies the binary's ELF machine instead; native builds still run the CLI.
4. **A gate could pass a code change without running any test**: T-10's repository had an invalid `project.yaml` (`browser_tests.paths`), so verification fell back to a proposal with no test command and the gate passed on build and review alone. A code change verified without a test step now always needs an approval (§56), and standard-library Python projects are detected (`python -m unittest` when `test_*.py` files exist).
5. **The orchestrator could not see its project**: the read-only checkout at `/projects/<slug>` was mounted, but Claude Code limits file tools to its working directories and the context never named the path, so the orchestrator raised a high-impact assumption ("no workspace mounted"). Orchestrator steps now pass `--add-dir /projects/<slug>` and the context names the path.
6. **The real-Docker test suite and smoke test 4 assumed the main stack**: the suite shared the operator's stack name, so the live control plane's orphan cleanup removed its workers mid-test; it now runs as its own stack and derives task-scoped names from it.
7. **Smoke tests 2–8 collided with the operator's Hermes port** (added in Phase 9): each now uses its own `HO_HERMES_DASHBOARD_PORT`.
8. Backup and check scripts: SQLite backup of a WAL database needs a writable mount; `head -n -N` is not portable; `grep -q` under `pipefail` misreports; unset optional settings ended `check.sh` silently; Docker Desktop cannot store a second platform of an image digest it already holds, so the multi-architecture check verifies the official Hermes digest's platform list instead of running it.

## Codex review (`/codex:review --base main`)

Five findings, all fixed: an approval for one version could deploy another (the script now checks the approved target before consuming it, and again after); restore did not bring back the execution image allowlist (now restored from the backup); restoring across a newer schema could fail half-way (now restores into a fresh database); cache trimming could race worker creation (now under Agent Manager's creation lock); the ARM64 check did not map `aarch64`.

Second review (commits after the first review): three findings, all fixed with tests. Projects whose tests sit in a plain directory (no `__init__.py`) got `python -m unittest`, which finds nothing there (Python 3.12 exits 5, so the step failed rather than passed); detection now emits `python -m unittest discover -s <dir> -v`. A review retry could call Git Service inside the transaction recording the finished execution, so a Git Service outage undid that record and stopped reconciliation; launches now wait in the queue while a platform service is down — and the regression test exposed that a queued subtask review was launched as a plain execution request and failed, now fixed. Resources created before stack labels existed were invisible to the upgraded stack's cleanup; unlabeled managed resources now belong to the main stack.

## Known limitations

- The acceptance run used a local repository; GitHub pull-request merges are verified against a local bare remote and a `gh` stand-in (Phase 5), not a real GitHub repository.
- Delivery to a real chat channel needs the operator's channel (docs/hermes.md).
- Multi-instance control planes are fenced but not exercised.
- `scripts/check-multiarch.sh` runs the other architecture under emulation; native Linux hosts were not available.
