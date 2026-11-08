# Fewer blocked tasks validation

Implements [docs/design/fewer-blocked-tasks.md](../design/fewer-blocked-tasks.md), following a review of every blocked, paused or cancelled task (T-1 to T-18: 77 transitions in 12 tasks). Delivered in PRs #102 to #109, plus configuration PRs in the project repositories. The design's one remaining item is the Dashboard view of the checks, which waits for an approved pen.dev design.

## What changed

| PR | Change |
| --- | --- |
| #102 | Task repair and Quality Gate contract (already deployed, now committed). Fixes receive the latest non-approving subtask review. A fix that leaves the reviewed commit unchanged is not reviewed again; a second one blocks the task (T-15 loop). |
| #103 | Subtasks of finished tasks hold no scope (T-15-1 held T-18's launch). |
| #104 | A Quality Gate `ui_design` failure makes the design required, so REQUEST_UI_DESIGN can satisfy it (T-18 deadlock). |
| #105 | `ci_checks` removed from the gate; the key is accepted and ignored. |
| #106 | Build, tests, lint, typecheck and security are hard policy. A configuration without a command for each is not offered for PROJECT_READY. Template projects get one setup task. New `ho-odoo-typecheck`. |
| #107 | Worst-case contract; developer preflight (`ho-verify` with the gate's steps, sent back at most twice without a review cycle); tasks wait in BACKLOG while their project lacks a mandatory command. |
| #108 | CMD-H07 and CMD-H09 judge program words only. MEDIUM and LOW findings are follow-ups. Closed acceptance criteria at planning. A dependency on a CANCELLED or FAILED task blocks the task visibly; retrying drops it. |
| #109 | The python toolchain ships pinned ruff, mypy and bandit. |

Project repositories: odoo-commercial_properties #21 and odoo-job_finder #29 (`ho-odoo-typecheck`); odoo-base #4 (module template) and #5 (lint, typecheck and security commands with two import-order fixes and an inline `# nosec B108`).

## Evidence

- Unit: 498 passed. Integration: 356 passed (final run of #108, which contains #107). Each PR passed the full suites before review; counts per PR are in their descriptions.
- `ruff`, `validate_schemas.py`: clean.
- Real Docker, `tests/docker/test_odoo_toolchain.py` with the rebuilt odoo image: 12 passed, including the new typecheck pass and fail cases.
- `ho-odoo-typecheck` on the real modules: job_finder (38 files) and commercial_properties (33 files) pass. A type error fails, `tests/` is ignored, and an invalid module name is refused.
- Classifier replay of the 76 recorded CMD-H07/CMD-H09 alerts (T-12 to T-15): 75 no longer fire. The remaining one is a stored command cut short before its closing quote, which the full runtime command would not trigger.
- Python toolchain test build: odoo-base's lint, typecheck, security and unittest commands pass in it.
- Deployed: the control plane and images of #102 to #109 are verified live (code markers, `ho-odoo-typecheck` in the odoo image, ruff 0.17.3, mypy 2.4.1 and bandit 1.9.4 in the python image); `ho health` reports every dependency ok.
- The #109 deploy left the control plane created but not started: the agent-manager, slow on a host busy after `make images`, was marked unhealthy within its 5-second start period, and `make up` failed. A second `make up` started it. The agent-manager healthcheck now has a 60-second start period. commercial-properties, job_finder and odoo-base were rescanned, offered for approval with every mandatory command, and approved by the person (PROJECT_READY).

## Not yet exercised

- No live task has run since the deployment. The preflight, follow-up acceptance and backlog wait are covered by integration tests with the fake Agent Manager, not yet by real providers.
- T-14 (commercial-properties) stays BLOCKED under its configuration pinned before these changes. It needs a gate exception or a recreated task.
- The Dashboard does not yet show the checks that will apply or why a project's configuration is incomplete (the scan response and `ho project scan` do).
- The generic toolchain ships no lint, typecheck or security tool. Projects on it must define commands their image can run, or move to a toolchain that has them.
