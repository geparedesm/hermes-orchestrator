# UI design approval validation

Implemented the headless pen.dev design stage, durable design packages, human approval binding, execution/dispatch enforcement, and opt-in image delivery.

## Evidence

- Full unit suite: 387 passed.
- Orchestration, clarifications and feature-delivery regression: 99 passed.
- UI design, execution and Git integration suite: 45 passed.
- UI lifecycle suite after dispatch hardening: 11 passed.
- UI design and notification integration suite: 23 passed.
- Final outbox ordering/non-starvation and notification regression: 12 passed.
- Sender tests using fake adapters only: 5 passed. Verified disabled-by-default behavior, explicit destination requirements, image hash checking, failure/retry behavior, and image receipts without approval authority.
- Ruff, JSON schemas/examples/profiles, shell syntax, `git diff --check`, and Compose configuration validation passed.
- Built isolated `hermes-orchestrator/claude-generic:ui-design-test` (image ID `sha256:b6fb4d52e866bdb7adbefac9fa1f4a48c513b58ddd039b6b3468e6751fcaa7fe`). Verified `pen 0.3.12` and `interactive --help` inside read-only, unprivileged containers with networking disabled. No active image lock was promoted.

The integration suites overlap; their counts are separate runs, not a count of unique tests.

## Covered invariants

Frontend implementation cannot start without a current approved package. Missing exports, changed package bytes, missing pen.dev credentials, and superseded requirements/plans cannot release the developer. Rejection carries feedback and the preceding design into the next proposal. Expired approvals renew against the same current package. Manual requests cannot impersonate the designer exception. Preexisting queued frontend work is checked again before dispatch. User attachments and the separately bounded four-file approved package both reach workers intact.

## Operational acceptance scope

Authenticated headless creation and export have passed, as detailed below. The owner authorized image delivery to the existing WhatsApp approval chat. Delivery is enabled in the deployed Hermes container. No design images have been sent to the real chat, and a live task spanning design, image delivery, human approval and implementation has not yet been exercised.

## Authenticated activation preparation

The owner configured a replacement CLI key outside chat and explicitly authorized its use in job-finder, commercial-properties and odoo-base. Each private secret file is owned by manager UID 10003 with mode 0600; directory ACLs grant traversal only. Configuration approvals 50, 51 and 52 were consumed by dashboard:operator. No credential values are included in source, build contexts, or this report.

A real headless run created a .pen and exported a valid 960×480 PNG. The same test succeeded in an internal Docker network through the platform CONNECT proxy after enabling NODE_USE_ENV_PROXY=1, with proxy evidence for api.pen.dev and fonts.gstatic.com. Native Export creates a directory containing <frame-id>.png, so the assignment now explicitly copies that file into the required desktop/mobile package filename. The updated UI integration suite passed all 12 tests.

Release ui-design-20261019 was deployed through scripts/update.sh after dashboard:operator approved UPDATE 53. The official update completed successfully and consumed that approval.

Release ui-design-20261019: all generic, Node, Python and Odoo worker builds completed successfully for Claude and Codex. Final Node, Python and Odoo provider images each passed an isolated, read-only, network-disabled pen 0.3.12 version check. The Odoo base passed its Odoo Server 19.0-20260926 check. The official update rebuilt and promoted the active image allowlist.

## Deployed verification

UPDATE 53 is CONSUMED; update 01a154b9-3b83-7fe1-816c-c4a8be4e180d finished successfully. Backup: backups/20261019T151308Z. All six services are healthy; the official check passed schema 0022, dependency health, orchestration plugin and Dashboard. Live Hermes has image delivery enabled with an explicit WhatsApp destination; its bridge reports connected. The new task-design CLI responds successfully. All three PEN_CLI_KEY files are readable by the running manager without displaying their contents.

A post-deployment check found init-secrets.sh reset the project-store ACL mask. Restored manager-only traversal and changed initialization to preserve operator-configured ACLs. Re-ran initialization and verified all three keys remained readable. No user frontend was changed and no real task spanning image delivery and human UI approval was exercised.

## T-9 project-read regression

T-9 stopped because the policy engine removed project_read from its DEVELOPER designer grant, although ui/portfolio.pen was tracked in the repository. The designer therefore received neither the source nor the existing design. Added an internal validated-designer flag at grant creation, a narrowly scoped schema exception, and an Agent Manager check requiring the fenced UI_DESIGN purpose and the task's own project. The mount remains an immutable tracked read view with workspace and Git access set to NONE.

Validation: 392 unit tests passed before the additional manager regression; the complete manager test file then passed all 8 tests. The 12 UI integration tests passed with new assertions requiring the project-read grant and view path. Policy tests reject other projects and write-capable requests; manager tests reject missing design fencing and cross-project reads. Ruff, schemas and diff checks passed. Deployment and a new T-9 design execution remain pending at preparation time.
