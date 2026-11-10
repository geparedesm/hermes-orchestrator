# Independent quota refresh validation

Date: 2026-11-10. Design: [independent quota refresh](../design/quota-refresh.md).

PR #113 adds bounded, account-only provider probes and PostgreSQL-fenced refresh scheduling. Runtime validation
found Docker rejects the probe's single-file local logger because compression defaults to enabled. PR #114
explicitly disables compression while retaining the existing 16 KiB bound.

Validation used Python 3.12, the existing development virtual environment, throwaway PostgreSQL and Redis, and
the pinned provider images. `PYTHONPATH` and `HO_SCHEMA_DIR` pointed at the validation worktree. Results:

- 536 unit tests passed on the merged feature; 32 focused probe/planning tests passed after the logging fix.
- 140 integration tests passed: quota refresh, orchestration, agents, and runtime/orphan recovery.
- Five real Docker quota tests passed with synthetic `hotest` identities and local CLI protocol stand-ins.
  They exercise both providers, output filtering, cleanup, refreshed-credential persistence, concurrent login
  preservation, and the legacy Claude token's profile requirement. No actual provider/model request is made.
- Four focused execution-image tests passed: the Claude credential mount, Codex refresh persistence, and both
  Claude saved-login refresh/interactive-renewal cases. The refresh cases use local stand-in CLIs and no network.
- Ruff, schema/example/machine-profile validation, and `git diff --check` passed.
- A real hardened container with the corrected logger started and exited successfully. Its initial cleanup
  timed out during concurrent image exports; the subsequent full quota tests completed normally.

The installed review command could not inspect the checkout because its bubblewrap namespace was unavailable.
A read-only review of the supplied final diff and relevant source found no actionable correctness/regression
bugs. That review did not independently execute tests or inspect files beyond the supplied material.

Real-container checks ran alongside the live stack's backup/image export. Docker fixture setup and removal were
slow; the five quota cases completed in 383.64 seconds. Only synthetic test identities were changed. Live task
credentials and workspaces were not used by these tests.

These results establish the isolation and account-protocol handling, not live account availability. Claude quota
readings require the operator's browser authorization through `make auth-claude`; a legacy setup token remains
usable for inference but cannot provide profile usage. See [operations](../operations.md#provider-quota-readings).
