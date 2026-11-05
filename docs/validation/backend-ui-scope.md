# Backend-only scope and visual approval

## Reproduction

After PR #100 was deployed, T-14's designer correctly understood its design-only role. It returned `completed` with no
design outputs because USER requirements v3 explicitly stated that the task has no interface changes and needs no UI
design. The platform nevertheless retained `ui_design_required=true` and blocked on the first absent export,
`design/changes.png`. No design file had actually been produced. T-15 remained active in its separate design execution.

## Correction

Respect an explicit human backend-only scope only when all planned files are concrete and nonvisual. Record the
decision with its requirements version, preserve explicit design choices and ambiguous/historical cases, and prevent
an agent-only assertion from clearing approval. Restore normal design enforcement when visual files enter the scope.
Check the authoritative integration diff at the Quality Gate so an undeclared visual change cannot use the backend
decision as permission to merge. Keep the current requirements version in the task row used during requirement updates.
The orchestrator receives the resulting scope decision and starts a normal developer execution for backend work.

No frontend design or application UI changes are included in this patch.

## Validation

- 469 unit tests passed, including Spanish/English declarations, visual paths, empty/wildcard scopes and contradictory
  instructions.
- Targeted integration tests reproduce a sticky UI requirement followed by a human backend revision and verify an
  actual writable DEVELOPER execution; they reject agent-only waivers and visual paths, preserve explicit UI choices,
  cover new tasks, and catch undeclared UI changes in the integration diff.
- Supplied-patch review found that a later agent-authored requirements version could remove the new final diff check.
  Fixed the guard to retain the check for any task with a recorded backend-scope decision, regardless of later
  requirements versions. A regression test explicitly creates that agent revision before evaluating the gate.
- Full integration and final image build results are recorded before release.
- Ruff and schema/example/machine-profile checks pass.

## Existing task

T-14 remains blocked until the approved platform update is installed and an authorized retry starts it again. Its
current USER requirements and backend plan provide the scope decision; no database state is rewritten manually and no
visual artifacts or approvals are fabricated. T-15 is not retried by this correction.
Update request 153 proposes `backend-ui-scope-20261106`; it remains subject to a concrete human approval.
