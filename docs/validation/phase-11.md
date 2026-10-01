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
| `make test-unit` | 233 passed |
| `make test-integration` | 147 passed |
| `make test-docker` | 48 passed (one real-provider test failed once while emulated builds saturated the machine and passed on rerun) |
| `make test-security` | 62 passed (39 integration + 23 real-Docker) |
| `scripts/smoke-phase11.sh` | all checks pass |
| `scripts/smoke-phase2.sh` … `smoke-phase10.sh` | all pass |
| `scripts/check-multiarch.sh` (linux/amd64 on Apple Silicon) | see below |
| Acceptance scenario (§90) with real Claude and Codex | see below |

### Failure injection (`scripts/smoke-phase11.sh`)

PostgreSQL stopped (readiness `unavailable`, then reconnection: new tasks accepted, scheduler resumed); Redis restarted (scheduling continues); dependency cache created, kept for the next execution, invalidated; maintenance; backup with checksums and no credential files, no secret values in Hermes's configuration, no service secret anywhere; restore (a task created after the backup is gone, earlier ones kept, Hermes plugin enabled); an update requested as an approval, refused before approval, refused for another version without consuming the approval, applied, recorded; a failing update rolled back automatically (previous version, state restored, `ROLLED_BACK` recorded, healthy).

### Architectures

All pinned base images are multi-architecture index digests (amd64 and arm64): Python, Debian, Node, Playwright, PostgreSQL, Redis, and Hermes. The macOS profile (`config/mac-m2-pro.yaml`) and the Linux profile (`config/linux.yaml`) are separate and selected by `HO_MACHINE_PROFILE`.

## Codex review (`/codex:review --base main`)

Five findings, all fixed: an approval for one version could deploy another (the script now checks the approved target before consuming it, and again after); restore did not bring back the execution image allowlist (now restored from the backup); restoring across a newer schema could fail half-way (now restores into a fresh database); cache trimming could race worker creation (now under Agent Manager's creation lock); the ARM64 check did not map `aarch64`.

## Known limitations

- The acceptance run uses a local repository; GitHub pull-request merges are verified against a local bare remote and a `gh` stand-in (Phase 5), not a real GitHub repository.
- Delivery to a real chat channel needs the operator's channel (docs/hermes.md).
- Multi-instance control planes are fenced but not exercised.
- `scripts/check-multiarch.sh` runs the other architecture under emulation; native Linux hosts were not available.
