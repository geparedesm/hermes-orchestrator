# Phase 6 Validation: Testing

**Date:** 2026-09-30  
**Machine:** MacBook Pro (Apple Silicon), Docker Desktop 29.4.0, `linux/arm64`, Docker VM with 16 GB RAM  
**Scope:** PHASES.md Phase 6 and its completion criteria: *required failures block `READY_FOR_MERGE`; successful checks produce auditable evidence in isolated environments.*

## What was built

| Deliverable | Location |
| --- | --- |
| Risk classification, required checks, test gaps, and runner steps (pure) | `packages/ho_core/src/ho_core/verification.py` |
| Test Runner script: planned steps, one retry for tests, `test_results.json`, one log per step | `workers/agent-base/ho-verify` (in every runner image) |
| Browser Runner image and page checks (screenshot, console, failed requests, trace) | `workers/browser-runner/` (official Playwright image v1.63.0, pinned by digest) |
| Project Compose test environments: render, sanitize, start on the task network, remove | `services/agent-manager/.../compose.py`, `docker_ops.py` (Compose v5.5.1, pinned by checksum) |
| Verifications, cross-reviews (review-result schema), Quality Gate | `services/control-plane/.../verification.py`, `schemas/review-result.schema.json`, migration `0005` |
| Test network policy: runners `NONE`, or the project's test allowlist; research presets only for development | `ho_core/policy/engine.py`, `config/defaults.yaml` (`research_domains`), `schemas/capability.schema.json` |
| Operator CLI: `ho tests show`, `ho verify`, `ho review run|show`, `ho gate evaluate|show` | `services/control-plane/.../cli.py` |

## Evidence

| Check | Command | Result |
| --- | --- | --- |
| Unit tests (adds risk and gap rules, Compose sanitizing, review schema) | `make test-unit` | 211 passed |
| Integration tests (adds 11 verification and Quality Gate tests; Phase 5 tests now go through the gate) | `make test-integration` | 75 passed |
| Real Docker: Compose services, network isolation, runner steps, browser runner | `make test-docker` | 47 passed |
| Phase 6 end-to-end on the Compose stack | `make smoke-phase6` | 20 of 20 checks passed |
| Phase 5, 4, 3, 2 end-to-end (regression) | `make smoke-phase5` … `make smoke` | all passed |
| Lint, schemas | `make lint validate-schemas` | clean |

### Isolated environments

- A project's own Compose file (PostgreSQL and a web app) was started for a verification: only the requested services and their dependencies, on the task's internal network only, published ports removed, `no-new-privileges`, memory, CPU, and process limits, `ho.*` labels. A test runner on the same task reached the database and the app but not the Internet; a runner of another task could not reach the database. Services and volumes were removed after the verification; the task network stays until the task ends.
- `network_mode: host`, `privileged`, added capabilities, the Docker socket as a bind mount, and `build:` were refused before any container started (N07). Unit tests cover the other forbidden keys and bind mounts outside the workspace.
- Runners have no Internet by default. With `network.testing: allowlist` they get only `test_allowed_domains`; development executions in restricted mode get the research presets (documentation, package registries, public GitHub), which runners never receive.

### Evidence that can be audited

- Each verification stores `test_results.json` and one log per step as artifacts, and one `test_runs` row per step (scope `FULL_SUITE`, `BROWSER`, or `POST_MERGE`). A test that passes only on retry is reported as flaky (`TEST_FLAKY`).
- The Browser Runner stored a screenshot, console messages, failed requests, and a Playwright trace per page; a page returning 404 failed the check.
- Each Quality Gate evaluation stores every requirement's status and evidence reference, the test gaps with the alternative evidence that ran, the risk, and the residual risk.

### Required failures block READY_FOR_MERGE

- The Quality Gate is the only transition from `QUALITY_GATE` to `READY_FOR_MERGE`, and a merge can be requested only with a passing evaluation for the integrated commit; the merge approval binds that evaluation.
- Blocking conditions shown in tests: failing tests (the smoke test's second change), no cross-review, a review from a provider that also developed the change (refused), HIGH or CRITICAL findings, unmet requirements, a target branch that moved since integration, and policy violations.
- A sensitive change with test gaps and a critical change need an explicit approval; approval resumes the task and re-evaluates the gate. A verification or CI run still in progress leaves the task in `QUALITY_GATE`.

### Real run with the operator's subscriptions

On the operator's stack and the `hello-api` project, task T-7 ("add `format_price`"):

| Step | Result |
| --- | --- |
| Codex 0.159.2 developer execution | `SUCCEEDED`; implemented the function and tests, ran them, committed `0934d91` |
| Integration and verification (Test Runner, `runner-python`) | `PASSED`; risk LOW (2 files, 25 lines); gaps: no build or lint command |
| Codex as reviewer | Refused: cross-review needs a provider other than the developers |
| Claude Code 2.1.280 cross-review | `APPROVED`, requirements met, two LOW findings (negative amounts put the sign after `$`; no type check on `cents`) |
| Quality Gate | `PASS`; tests PASS, build and lint UNAVAILABLE (recorded as gaps), cross-review, requirements, findings, conflicts, and policy PASS |
| Task | `READY_FOR_MERGE`; the MERGE approval is waiting for the operator |

The first Claude review failed with `error_max_structured_output_retries`: stripping annotations from the review schema also removed the findings' `description` property, leaving it required but undefined. Claude's own final answer pointed at the schema. Fixed in `adapters/base.py`, with a regression test, and the rerun succeeded.

## Decisions made in this phase

| Decision | Reason |
| --- | --- |
| No generated override file; the sanitized Compose model is started from stdin | Nothing is written into the project, and there is nothing to clean up in the repository. |
| `docker compose config` runs with an empty environment | A project's Compose files cannot interpolate Agent Manager's environment. |
| Builds in project Compose files are refused | A build runs repository instructions with network access; test services must use images. |
| `ports:` are removed, not rejected | Most development Compose files publish ports; removing them keeps services private without making those files unusable. |
| Test services also answer as `<service>.test` | Chromium forces HTTPS for HSTS-preloaded names such as `app` (a top-level domain); the browser check uses the `.test` alias for single-label hosts. |
| Runners use the machine profile's runner limits (`runners.test`, `runners.browser`) | Their resources are configured separately from agent workers (section 34); test services default to 1 GiB each. |
| One Test Runner execution runs all steps | Installed dependencies are shared between steps, with per-step results and logs. |
| Review findings use a separate result schema | Structured findings with severity are what the gate checks; the developer result schema has no place for them. |
| Policy violations (`COMMAND_HIGH_RISK`, denied policy decisions) need an explicit approval instead of failing permanently | The advisory classifier can flag legitimate commands; a human decides. |

## Known limitations

- `QUALITY_GATE` is reached by the orchestrator in Phase 7; the smoke tests and the real run set it directly. The smoke tests also record the cross-review directly, because a real review needs a provider login (the real run used one).
- Relevant-test runs by developers (`test_relevant`) are the developer agent's responsibility; the platform runs the full configured suite.
- CI evidence was verified only with the GitHub CLI stand-in (Phase 5).
- Installing dependencies needs network access: projects that need it set `network.testing: allowlist` with their registries until dependency caches (section 71) exist. Flutter and Dart packages cannot be resolved offline.
- Review findings are resolved by a new review of a newer commit; individual findings cannot yet be marked fixed or waived.
- Linux has not been run.

## Operator review

1. The MERGE approval for T-7 on `hello-api` is waiting: `docker compose exec control-plane ho approval list`, then `ho approval approve <id>`. Post-merge verification runs, and the task becomes `DONE`.
2. `ho tests show T-7`, `ho review show T-7`, `ho gate show T-7` show the evidence behind it.
