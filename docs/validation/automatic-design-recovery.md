# Automatic design assignment recovery

## Observed failures

- T-14 design executions received the generic developer brief requiring implementation, tests and commits in
  `/workspace`, although the design grant intentionally mounted only a read-only project view. Repeated reports
  asked for permissions that this role must not have.
- T-15 design v5 reported that approved v4 was absent. Database inspection found all five v4 deliverables, and the
  failed execution's `design_package_id` referenced v4. Its complete implementation assignment and approval history
  obscured the actual design-only task.
- A plan revision invalidated open UI approvals even though `UIDesigns.matches` intentionally ignores plan versions.
  That could leave a matching PENDING design with an unusable approval.

## Changes

Designer-specific adapter framing; design-only feature context; explicit platform input/output version metadata;
bounded, audited recovery for verified assignment confusion; and preservation of visual decisions across technical
plan revisions. No frontend, worker permissions, project quality policy, approval authority or budget limits changed.

## Validation

- Adapter tests cover Claude and Codex designer briefs and retain the ordinary implementation brief.
- Integration tests exercise one automatic recovery followed by a bounded stop, verified reference package delivery,
  mandatory new visual approval, genuine prerequisite failures, and an unchanged pending approval after replanning.
- The full unit suite passes: 462 tests. The UI design integration suite passes: 23 tests.
- Ruff and JSON Schema/example/machine-profile validation pass.
- A review of the supplied complete patch and relevant source found no actionable P1/P2 regressions. The normal
  `codex review --uncommitted` could not inspect files because this host denies its bubblewrap loopback setup; that
  unsuccessful run was not treated as a review verdict. The supplied-patch review used no tools.
- The control-plane image builds as `hermes-orchestrator/control-plane:task-recovery-20261106`.

## Deployment and existing tasks

The code must pass review and the platform's human-bound merge/update approval process before deployment. Existing
T-14/T-15 are not rewritten in SQL or automatically marked approved. After deployment, authorized task retries use
the corrected path. T-15 has only four of sixty launches remaining; budget request 151 proposes twenty additional
launches and remains pending until a person approves it. Current requirements differ from v4's requirements: the v4
package is revision input, not an approval for a new version.
