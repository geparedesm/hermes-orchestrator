# Clarifying questions and supervised plans

Status: proposal.

During PLANNING, the orchestrator may ask the person a few multiple-choice questions when an answer would change
what is built. Optionally, the plan is shown to the person for approval before any subtask starts.

## Data

- **Migration `0015`.**
  - A `clarifications` table: id, task, `number` (from the approval number sequence, so chat references are
    unique), the requirements version it was asked against, `questions`, `answers`, state
    (`PENDING`, `ANSWERED`, `CANCELLED`), `answered_by`, and timestamps.
  - Each question has an id, a text, a `why` (what it changes), 2 to 4 options of which exactly one is
    `recommended`, and `allow_other`.
  - A `PLAN` approval action.
  - `plan_versions.approval_state`: `NOT_REQUIRED`, `PENDING`, `APPROVED`, or `REJECTED`.
- **`project.yaml`** gains `planning: {questions: true, approve_plan: false}`.
  - A task may turn `approve_plan` on when it is created: a tighten-only override, shown as a checkbox in New task.
  - A task cannot turn it off.

## Orchestrator

- A new action, `ASK_QUESTIONS`, with 1 to 5 questions. It is accepted only in PLANNING, at most once per
  requirements version, and before a plan exists for that version.
- The step instructions say:
  - ask only when an answer changes what is built or how it is tested;
  - never ask what the repository, the request, or the attachments already answer;
  - always mark a recommended option.
- The control plane stores the round and moves the task to `APPROVAL_REQUIRED` (a waiting state: it consumes
  neither runtime nor budget). Chat and Dashboard show a question card.

## Answering (human principals only, AD-10; never an LLM tool)

- `POST /v1/tasks/{key}/clarifications/{id}/answer` takes either `{answers: {q1: {option: "b"} | {text: "…"}}}` or
  `{use_recommended: true}`.
- The answers are appended to the task's requirements as a new version ("Clarifications" section, each question
  with its answer). The `REQUIREMENTS_REVISED` trigger resumes PLANNING, so the plan, reviewers, and Quality Gate
  all see them.
- Refused when:
  - the task is no longer waiting on that round (cancelled, or answered twice);
  - an option does not exist;
  - free text is given where it is not allowed.
- In chat, `r 21 1a 2b 3: only remote jobs` becomes `/orch answer 21 1a 2b 3: only remote jobs` through the
  existing short-reply hook; `r 21 ok` uses the recommended answers.

## Supervised plan

- When `approve_plan` is on, SET_PLAN records the plan version with `approval_state = PENDING`. It requests a `PLAN`
  approval bound to the plan's digest. Its details list the subtasks: title, kind, risk, provider, dependencies, and
  files.
- The task waits in `APPROVAL_REQUIRED`. Launch code refuses to start a subtask whose plan version is not
  `NOT_REQUIRED` or `APPROVED`.
- **Approve:** the plan starts.
- **Reject (with an optional note):** the note becomes orchestrator input and PLANNING produces a new plan. A new
  SET_PLAN invalidates the previous `PLAN` approval.

## Tests

- **Integration**, with fake agent results:
  - questions are asked, then answered (by option, by text, or with the recommended answers), the requirements are
    revised, and planning resumes;
  - a second round for the same requirements version is refused;
  - an LLM or non-approver principal cannot answer;
  - an answer after cancellation is refused.
  - With `approve_plan`: nothing launches until the plan is approved; rejection re-plans; a new plan invalidates
    the old approval; a task override can turn it on but not off.
- **Unit:** schema of the new action, chat reply parsing, and the requirements rendering of answers.

## Risks

- **An orchestrator that asks too much:** one round per requirements version, at most five questions, and the
  instructions above.
- **Tasks waiting for answers:** they sit in Needs you, and the digest reminds the person. They are never answered
  silently.
- **A plan approval that goes stale:** it is bound to the plan digest, and any new plan invalidates it.
