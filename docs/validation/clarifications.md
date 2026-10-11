# Validation: clarifying questions and supervised plans

Design: [docs/design/clarifications.md](../design/clarifications.md). Date: 2026-10-12.

## What was built

- **Orchestrator.**
  - A new action, `ASK_QUESTIONS`: 1 to 5 questions, each with 2 to 4 options, exactly one recommended, a `why`, and
    `allow_other`.
  - It is accepted only in PLANNING and within `planning.max_question_rounds` (default 1) per planning episode. An
    episode starts with the request or a person's revision; answers do not start a new one.
  - Later actions of the step that asked are not applied.
  - The step instructions say to ask only when an answer changes what is built or tested.
- **Answering** (people only): `POST /v1/tasks/{key}/clarifications/{id|number}/answer` and
  `POST /v1/clarifications/{number}/answer`. The body is either an option or a text per question, or the
  recommended answers. In one transaction:
  - the answers are stored structured;
  - they are appended to the requirements as a `CLARIFICATION` version (refused when that would exceed the size
    limit);
  - the task goes back to PLANNING.
  The orchestrator's context has its own section with every answer, outside the 5,000-character requirements cut.
- **Supervised plans** (`planning.approve_plan`, or a task's "Review the plan before work starts"; tighten-only).
  - SET_PLAN asks for a `PLAN` approval bound to the plan version and digest and to the requirements version and
    digest. The task waits from where it planned.
  - Every developer start checks that the current plan is explicitly approved: action, `_develop`, and queued
    dispatch. A new plan drops queued developer launches.
  - **Approve:** the plan starts. **Reject:** the note goes to the orchestrator, which plans again.
  - A revision cancels pending rounds, invalidates the `PLAN` approval, and returns the task to where the wait
    started. Retries keep `approve_plan`. Ending a task cancels its pending rounds.
- **Chat.**
  - A question notification lists options per question, with the recommended one marked.
  - Answer with `r 21 1a 2b 3: own words`, or `r 21 ok` for the recommended answers.
  - `/orch answer` does the same.
- **Dashboard.**
  - A question card with selectable options (the recommended one preselected), "Other" with a text field, and
    "Send answers" or "Use the recommended answers".
  - It shows in Needs you, Approvals, and the task panel, where "Your answers" lists past answers.
  - The `PLAN` decision card lists each subtask's title, kind, risk, provider, dependencies, and files.
  - New task has the "Review the plan before work starts" checkbox.

## Tests

| Suite | Result |
| --- | --- |
| Unit | 323 passed (chat parsing, `/orch answer` is a human action, plugin create body) |
| Integration | 224 passed |
| `tests/integration/test_clarifications.py` | 18 passed against the real state machine (list below) |
| Dashboard | rendered in jsdom with fixtures, without errors; screenshot reviewed |

The 15 clarification tests cover:
- asking, answering (by option, by text, or with the recommended answers), and planning resuming with the answers;
- no second round after answering;
- a revision cancelling a round and opening a new episode;
- invalid answers, and agents, refused;
- a cancelled task taking no answers;
- later actions of the asking step not applied;
- long requirements keeping the answers in context, and oversized answers refused;
- a supervised plan starting nothing until approved;
- rejection re-planning with the note;
- a revision invalidating a pending `PLAN` approval, and then starting nothing until a new plan is approved;
- the task-level opt-in;
- retries keeping supervision;
- cancelling removing the question card;
- a revision after approval requiring a new plan approval;
- answers reaching reviewers, with earlier answers marked superseded after a revision;
- valid manifests for tasks with answers and plan approvals.

## Reviews

- **Codex adversarial review of the design** found 4 issues, all adopted before building:
  - an explicit transition back to PLANNING;
  - bindings and invalidation of plan approvals and rounds on revision;
  - answers that survive the context and size limits;
  - a round limit that answering does not reset.
- **Codex review of the code** found 2 P1 and 3 P2, all fixed:
  - **P1:** missing approval state counted as permission. Now a supervised task needs an explicit approval of its
    current plan.
  - **P1:** queued developer launches could bypass the check. Now dispatch checks it, and a new plan drops them.
  - **P2:** waits that began outside PLANNING now resume their recorded state on revision.
  - **P2:** retries keep `approve_plan`.
  - **P2:** ending a task cancels its pending rounds, and the cards of finished tasks are hidden.
- **Second Codex review** found 1 P1 and 3 P2, all fixed:
  - **P1:** a revision after approval left the plan approved for the old requirements. Now any revision clears the
    approval, and the orchestrator is told to submit the plan again.
  - **P2:** the manifest schema now accepts `CLARIFICATION` sources and the `PLAN` and `CREATE_CHILD` actions.
  - **P2:** integration and subtask reviewers receive the current episode's answers as part of what was asked.
  - **P2:** answers from before a revision are shown as earlier answers that the revision overrides.
