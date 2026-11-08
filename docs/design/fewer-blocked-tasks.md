# Fewer blocked tasks

Status: plan agreed with the person on 2026-11-08. Sections 1 and 2 implemented; sections 3 and 4 pending.

Evidence: 77 BLOCKED, PAUSED_BUDGET, CANCELLED or FAILED transitions across 12 of 18 tasks (T-1 to T-18). Infrastructure, post-merge, design-flow and review-loop causes are already fixed (PRs up to #104). The changes below address the causes that remain. Each change ships with tests in its own PR, in this order.

## 1. Remove `ci_checks`

Projects have no CI workflows, so the check always reports UNAVAILABLE and never blocks. It is still enabled by default, tighten-only, and announced to agents as a requirement.

- Remove it from `schemas/project.schema.json`, `config/defaults.yaml`, `TIGHTEN_ONLY` in `ho_core.config`, the gate (`verification.py`), the assignment contract (`gate_context.py`), the Dashboard read models, and `templates/generic`.
- Accept and ignore `quality_gate.ci_checks` in existing `project.yaml` files, with a config warning, so registered projects keep validating. Stored evaluations keep their historical `ci_checks` rows.

## 2. Every project has every command check

Mandatory for every project: `build`, `tests`, `full_suite`, `lint`, `typecheck` and `security_checks`. `browser_tests` stays required only for tasks that change UI, and `docs_updated` only for changes to public behaviour (README or API).

- Defaults become `true` for all six, and project layers can no longer turn them off (they are already tighten-only upward).
- Onboarding: a scan proposes a command for every mandatory check. A project without a command for one of them cannot reach PROJECT_READY; the person approves the proposed commands.
- Odoo toolchain: new `ho-odoo-typecheck <module>` next to `ho-odoo-lint`. It runs mypy over the module, treats `odoo` as `Any` (`--ignore-missing-imports`, `follow_imports=silent`), and pins its version in `workers/versions.env`. job_finder moves from its file list to the wrapper. commercial-properties gets the command through a config change that the person approves.
- A project created from a platform template (`child_creations` kind PROJECT) may become PROJECT_READY without commands; until it has them it accepts one open task, which sets them up (the contract tells its agents). Child modules get no exception: their template must carry every command.
- Existing tasks keep their pinned configuration. T-14 needs either the gate exception or a recreation after the config change.

## 3. Agents know the full gate from the start

The assignment contract (`gate_context.py`, since #102) already goes to the orchestrator, developers and reviewers.

- Worst-case contract: risk can only rise when the integrated diff is evaluated (T-14 became HIGH and then required typecheck). The contract states the requirements of the highest risk the declared and configured sensitive paths can reach, and marks which ones apply only at that risk.
- Developer preflight: before reporting `completed`, the developer runs every required command in its workspace (`ho-verify --preflight`, the same planner the gate uses) and fixes failures. The result carries the preflight outcome, and a run that reports failing checks goes back as a fix without a review cycle.
- Creation check: when a task is created, the control plane computes the worst-case requirements. If the project lacks a command one of them needs, the task is created BLOCKED with that reason and the exact missing command, instead of failing at the gate hours later.
- Dashboard: the new-task form and the task view show the checks that will apply. This is UI work: it is designed in `ui/*.pen` first (Before, After and Changes frames) and built only after the person approves the design.

## 4. Other causes of blocks

- Command classifier false positives (`ho_core.policy.commands`). CMD-H09 matched Odoo's `.sudo()` and the Spanish word "su"; CMD-H07 matched "Docker" inside test strings. Together they produced 76 advisory violations in 8 tasks, and each one required a gate exception. Rules match command words only: not inside quoted strings, heredoc bodies or Python source passed with `-c`, not method calls (`.sudo(`), and `su` only as a command word. Each rule gets regression tables taken from the recorded events.
- MEDIUM findings do not block. Only HIGH and CRITICAL block, as hard policy already requires. MEDIUM findings become follow-ups listed on the PR and the task. commercial-properties drops MEDIUM from `block_on_findings` through a config change the person approves.
- Closed acceptance criteria. When a requirement admits unbounded wording or behaviour (T-15 eligibility, T-8 exact behaviour), the planning instructions require the orchestrator to write a table of cases or to ask the person (ASK_QUESTIONS). Reviewers judge against that table; findings outside it are not blocking.
- Dependencies on cancelled tasks. A task whose dependency is CANCELLED or FAILED becomes BLOCKED with the reason and the dependency's key, instead of staying in BACKLOG (T-16). The person can re-point the dependency or drop it.

## Validation

Unit and integration tests per change; `make lint validate-schemas`; a smoke run on a throwaway stack; then `docs/validation/` evidence and design-doc updates (ARCHITECTURE, SECURITY_MODEL for the classifier).
