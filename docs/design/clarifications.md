# Clarifying questions and supervised plans

Status: built.

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

- A new action, `ASK_QUESTIONS`, with 1 to 5 questions. It is accepted only in PLANNING, before a plan exists, and
  within the round limit of the planning episode (see below).
- The step instructions say:
  - ask only when an answer changes what is built or how it is tested;
  - never ask what the repository, the request, or the attachments already answer;
  - always mark a recommended option.
- The control plane stores the round and moves the task to `APPROVAL_REQUIRED` (a waiting state: it consumes
  neither runtime nor budget). Chat and Dashboard show a question card.

## State and version contract

Changes made after Codex's adversarial review of this proposal:

- **Entering and leaving the wait.**
  - Questions and supervised plans both wait from PLANNING: `PLANNING → APPROVAL_REQUIRED`, with the state machine
    recording `resume_state = PLANNING`.
  - With `approve_plan` on, SET_PLAN stops before its usual `PLANNING → QUEUED`.
  - Answering, approving, or rejecting performs an explicit `Tasks.transition` back to PLANNING. Only then is the
    wake-up event recorded.
  - An approved plan then continues exactly as an unsupervised SET_PLAN does (`→ QUEUED`, launches). A rejected plan
    records its note as orchestrator input and plans again.
- **Everything is serialized on the task row.**
  - Answer, plan decision, requirements revision, cancel, retry, and launch checks all lock the task row (`FOR UPDATE`).
  - A round or plan approval is honored only when it is still the task's current one.
- **Bindings.**
  - A `PLAN` approval binds the task, the plan version and its digest, and the requirements version and its digest.
    The configuration hash and policy version come from the approval's state hash.
  - The approval is consumed against the current values before the plan starts. Launch code also requires its plan
    version to be `NOT_REQUIRED` or `APPROVED`.
  - A clarification round binds the requirements version it was asked against.
- **Revisions invalidate.**
  - `Tasks.revise` (a person changing the request) cancels pending clarification rounds and invalidates open `PLAN`
    approvals.
  - A task waiting on either returns to PLANNING, so the new requirements are planned, and asked about if needed.
- **Answers are never lost.**
  - Answers are stored structured in `clarifications`.
  - The orchestrator's context has its own section, "Clarifications answered by the person", read from that table
    and not subject to the 5,000-character cut of the requirements text.
  - The requirements artifact also gets them appended. If the result would exceed its size limit, the answer is
    refused with a clear error instead of being truncated.
- **A real limit on rounds.**
  - At most one round (`planning.max_question_rounds`, default 1, at most 3) per planning episode.
  - An episode starts with the task's request or a person's revision; versions created from answers do not reset
    it. So the orchestrator cannot ask again just because it was answered.

## Answering (human principals only, AD-10; never an LLM tool)

- `POST /v1/tasks/{key}/clarifications/{id}/answer` takes either `{answers: {q1: {option: "b"} | {text: "…"}}}` or
  `{use_recommended: true}`.
- The answers are appended to the task's requirements as a new version ("Clarifications" section, each question
  with its answer; source `CLARIFICATION`). In the same transaction, the task returns to PLANNING. The plan,
  reviewers, and Quality Gate all see the answers.
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

- **Integration**, with fake agent results, against the real state machine:
  - revising, cancelling, or retrying the task while a round or plan approval is pending;
  - a second round right after answering is refused;
  - answers appended to long requirements stay in context, and oversized ones are refused;
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
