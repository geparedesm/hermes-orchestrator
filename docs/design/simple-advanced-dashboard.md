# Simple and Advanced Dashboard views

The default view explains task status and the next useful step for nontechnical users. All interface copy remains English.
Advanced preserves the existing technical tabs, original failure reason, execution tables, detailed budget, Git evidence and history.
The browser remembers the view preference; it does not grant permissions or change the task state machine.

## Design

Pencil source: `ui/hermes-dashboard.pen`. The user approved applying the presented Simple/Advanced direction.
Task concepts: m0aMkd (Simple), zbudC (Advanced), Svm9K (mobile). Home/list concept: dC6tU.
State/action contract: l41gMo. Additional task flows: cSWzp (design decision), MsXLa (test/apply),
K2TKt (queue), xfuXl (working), feOep (unavailable data), QrPiG (restricted), uvbTQ (completed).
The scenario designs were completed in Pencil before frontend implementation. Existing frames were preserved.

## Information and evidence

The plugin adds a read-only `guidance` field to the existing authenticated task-detail response, using its existing transactional
orchestration snapshot. This requires no new control-plane endpoint, model, LLM request or additional API call.
It includes current blockers, responsible parties, suggested resolution order, current preview availability, retry/resume prerequisites,
latest meaningful progress and the time this snapshot was presented. It leaves all original evidence available in Advanced.

Budget exhaustion is taken from the current budget, not a historical task reason. Missing review input is recognized only from
currently blocked subtask reports with known missing-review wording. It is a report of missing input, not a claim that the underlying
review is absent from the platform. Unknown blockage reasons remain unknown and are directed to the operator, rather than guessed.
Queue evidence is available in Advanced; the view does not fabricate a retry time or promise when an agent will finish.

Simple shows all identified impediments and does not offer a blind retry. Advanced permits an operator to retry an otherwise
unclassified blockage after reading the original evidence, but suppresses known futile retry/resume actions (missing review input,
exhausted allowance, active work, or unavailable context). Server-side authorization and validation remain decisive.
Simple is not a substitute for missing task evidence or a repair to task execution.

## Interaction

Home separates decisions from tasks needing attention; task lists hide agent metrics. System tools remain in Advanced.
A task's preview is prominent in Simple only when verified, unexpired and available; an absent environment produces no placeholder card.
Design approval waits for both current images to load and their delivery receipts to succeed. A saved decision is explicitly acknowledged.
Technical approval evidence remains available in Advanced, and action-bound approval checks remain enforced by the control plane.

Failed refreshes keep the last good content with a visible warning and disable task/decision controls until a successful refresh.
Overlapping polling replies and replies to closed/changed tasks are discarded. The task panel traps keyboard focus, supports Escape
and returns focus to the opening control. Mode selectors have explicit pressed state and work without browser storage.
