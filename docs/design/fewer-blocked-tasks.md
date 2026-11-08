# Fewer blocked tasks

Status: plan agreed with the person on 2026-11-08. Sections 1 to 4 implemented and deployed, except the Dashboard view of section 3 (it needs a pen.dev design first). Evidence: [docs/validation/fewer-blocked-tasks.md](../validation/fewer-blocked-tasks.md).

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
- Toolchains must ship the tools: the python toolchain carries pinned ruff, mypy and bandit (`PYTHON_QUALITY_PYTHON`, the odoo toolchain's versions), so a Python project such as odoo-base can define lint, typecheck and security commands. node projects use their own scripts (`npm run lint`, `tsc`); the generic toolchain has none, so its projects define commands their image can run or move to a toolchain that has them.
- Existing tasks keep their pinned configuration. T-14 needs either the gate exception or a recreation after the config change.

## 3. Agents know the full gate from the start

The assignment contract (`gate_context.py`, since #102) already goes to the orchestrator, developers and reviewers.

- Worst-case contract: risk can only rise when the integrated diff is evaluated (T-14 became HIGH and then required typecheck). The contract states the requirements of the highest risk the declared and configured sensitive paths can reach, and marks which ones apply only at that risk.
- Developer preflight: every developer run receives the gate's steps as `step-NN-<name>.sh` inputs (the same planner the gate uses) and is told to run `cd /workspace && ho-verify` before reporting `completed`. When its `/output/test_results.json` is missing or has a failing step, the run goes back to its developer with the failing steps and their log tails (PREFLIGHT_FAILED), without a review cycle, at most twice in a row; then the reviewer sees the change anyway, so the preflight cannot loop.
- Creation check: when a task is created, and on every backlog pass, a project whose active configuration lacks a mandatory command keeps the task in BACKLOG with the reason and the exact missing command (TASK_WAITING_FOR_CONFIG, once). It is not BLOCKED: leaving BLOCKED needs a retry and would keep the old pinned configuration, while a waiting BACKLOG task starts by itself, under the new configuration, once the person approves it.
- Dashboard: the new-task form and the task view show the checks that will apply. This is UI work: it is designed in `ui/*.pen` first (Before, After and Changes frames) and built only after the person approves the design.

## 4. Other causes of blocks

- Command classifier false positives (`ho_core.policy.commands`). CMD-H09 matched Odoo's `.sudo()` and the Spanish word "su"; CMD-H07 matched "Docker" inside test strings. Together they produced 76 advisory violations in 8 tasks, and each one required a gate exception. Rules match command words only: not inside quoted strings, heredoc bodies or Python source passed with `-c`, not method calls (`.sudo(`), and `su` only as a command word. Each rule gets regression tables taken from the recorded events.
- MEDIUM findings do not block. Only HIGH and CRITICAL block: hard policy drops MEDIUM from `block_on_findings`, and the gate ignores it in configurations pinned earlier. A subtask review that requests changes with its requirements met and only MEDIUM or LOW findings accepts the work and records them as follow-ups on the task (REVIEW_FOLLOW_UPS). No change to commercial-properties' file is needed.
- Closed acceptance criteria. When a requirement admits unbounded wording or behaviour (T-15 eligibility, T-8 exact behaviour), the planning instructions require the orchestrator to write a table of cases or to ask the person (ASK_QUESTIONS). Reviewers judge against that table; findings outside it are not blocking.
- Dependencies on cancelled tasks. A BACKLOG task whose dependency is CANCELLED or FAILED becomes BLOCKED with the reason and the dependency's key, instead of staying in BACKLOG (T-16). Retrying it is the person's decision to go on without that dependency: the dead dependencies are dropped (DEPENDENCY_DROPPED) and the task returns to BACKLOG. Cancelling it is the other choice.

## Validation

Unit and integration tests per change; `make lint validate-schemas`; a smoke run on a throwaway stack; then `docs/validation/` evidence and design-doc updates (ARCHITECTURE, SECURITY_MODEL for the classifier).
