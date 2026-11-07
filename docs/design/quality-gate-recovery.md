# Quality Gate context and task recovery

Agent assignments receive a contract generated from the task's pinned effective configuration and the same verification planner used for integration. The contract lists configured commands, required checks, risk escalation, test gaps, blocking finding severities, exact-commit review/integration requirements, and human decisions. It includes the last failed evaluation and the corresponding review findings as historical evidence; a correction must pass a new evaluation. Unknown scope is explicitly provisional. This reduces preventable failures; it cannot guarantee that an implementation meets its acceptance criteria.

Accepted corrections now proceed from FIX_REQUIRED through RUNNING to integration, automated verification, integration reviews and the Quality Gate. This closes the T-14/T-15 loop where SUBMIT_FOR_QUALITY_GATE was refused and REQUEST_EXECUTION was then refused because the subtask was already ACCEPTED. The scheduler does not automatically resubmit unchanged failures from FIX_REQUIRED.

## Recovery action

`POST /v1/tasks/{key}/repair` (also `ho task repair T-14`) records TASK_REPAIR_REQUESTED with the authenticated principal, blockage reason and resume state. It retries the same blocked task through the existing retry path. Subsequent orchestrator steps receive the original blockage independently of their event cursor, current subtask state, rejected actions and the Quality Gate contract. Accepted work is preserved and integrated deterministically when possible; otherwise Hermes diagnoses the blockage and proposes validated recovery actions. Repeated idle steps remain bounded.

The action rejects tasks outside BLOCKED, active work, and exhausted allowance. Idempotency follows the existing task-action API. It does not grant approvals, reset budget, lower gate requirements, merge, change credentials, or let project workers edit the platform. Post-merge recovery continues to use the existing retry hook for verification of the already merged commit.

## Approved Dashboard design

Placement: the blocked task's existing explanation panel, visible in Simple and Advanced, alongside its recorded cause. Primary action: **Resolve with Hermes**. Helper text: **Hermes will check the blockage and try to continue using the remaining allowance.** After submission: **Recovery requested**; refresh the current task state. While submitting: **Requesting recovery…** and disable repeat clicks. Display API errors next to the action and keep the blocked state visible until a successful response.

Show the action for BLOCKED tasks with current context and no active executions. When allowance is exhausted, show the existing allowance action instead. Authentication, approval and paused states keep their existing specific actions. The backend validates eligibility again when clicked.

The user approved v1 before implementation. Pencil frames: `ze0kd` (Before), `AMTeH` (After), `g30ib` (Changes, shown first), `b5NBhn` (interaction states). Earlier versions were preserved. Exported previews are in `ui/exports/task-recovery-v1/`. The button uses the existing React components, styles, freshness guards and Dashboard task-action proxy.

## Fixes that change nothing (T-15)

T-15 was cancelled by its person after one commit was cross-reviewed nine times, and review budgets were raised four times. Two defects combined. First, a fix the orchestrator started after REJECT_SUBTASK received only the last Quality Gate's findings. Those were from an older commit and already fixed, and the developer never saw the subtask reviewer's findings. The orchestrator's step did not list them either. Second, a developer run that added no commits was reviewed again because the workspace had commits from earlier runs. Every new review of the unchanged commit found other wording issues, so the subtask hit the review limit again.

Now a fix run, however it starts, receives the latest non-approving subtask review in full. The step prompt shows that review under each BLOCKED or FIX_REQUIRED subtask, so REJECT_SUBTASK guidance is based on it. When a developer leaves the last reviewed commit unchanged, the commit is not reviewed again. The event FIX_WITHOUT_CHANGES is recorded, the subtask becomes BLOCKED, and the orchestrator receives the open findings. If a second run since the person last acted also leaves that commit unchanged, the task becomes BLOCKED: the developer and the reviewer disagree, and the person decides. Requirements that admit unbounded wording (T-15's free-text eligibility rule) should be bounded by an explicit acceptance table. The platform can stop the loop, but it cannot make such a requirement converge.
