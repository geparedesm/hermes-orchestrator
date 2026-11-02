# Simple and Advanced Dashboard validation

## Delivery

The user approved applying the Simple/Advanced design direction. The remaining state designs were completed in
`ui/hermes-dashboard.pen` before frontend implementation; see `docs/design/simple-advanced-dashboard.md` for frame IDs.
Implementation was developed in the isolated `codex/simple-advanced-dashboard` worktree.

The deployment changes only the mounted orchestration Dashboard plugin, documentation, tests and Pencil design.
No control-plane image rebuild, database migration, task retry, budget increase, approval decision or protected-branch merge was performed.
At initial activation the live checkout contained the deployment changes without a commit or pull request.
The design and implementation are committed on `codex/simple-advanced-dashboard` and published in
[PR #96](https://github.com/geparedesm/hermes-orchestrator/pull/96). The PR targets `main`; integration is pending.

## Automated checks

- Full unit suite: **445 passed**.
- Final focused Dashboard tests: **46 passed**.
- Ruff passed on all changed Python sources/tests; `node --check` passed; `git diff --check` passed.
- Browser Runner exercised the actual plugin JavaScript/CSS with the platform's installed React version and isolated API fixtures.
  All requests were intercepted, with networking disabled. The test did not authenticate to the live Dashboard or mutate real tasks.
- Verified Simple default, Advanced technical evidence, remembered preference, focus containment, Escape closure,
  suppression of known futile retries, and absence of unavailable test-version placeholders.
- Verified no horizontal overflow at **320, 390 and 768 px**.
- Simulated status-fetch failure preserved content, showed a warning and disabled actions until refresh succeeded.
- Simulated missing mobile design image kept approval disabled. Successful image receipt and a fixture-only approval
  produced an explicit acknowledgement and refreshed the decisions count. No live approval was submitted.
- Browser run reported no JavaScript page errors. Screenshots and the React test bootstrap are in `/tmp/hermes-dashboard-review/`.

## Activation and live checks

Previous Dashboard files were backed up in `/tmp/hermes-dashboard-before-simple-advanced-20261102T182712Z/`.
The manifest records which files existed and the deployed content hashes. To roll back, restore the saved files,
remove only new files identified by that manifest, then restart the `hermes` service.

The plugin files were activated and only `hermes` was restarted. All six platform services subsequently reported healthy;
the Dashboard providers endpoint returned HTTP 200. The deployed Dashboard task handler was exercised with its configured
operator API identity against live T-13: state BLOCKED, current guidance context, missing-review-input blocker and retry disabled.
The current allowance no longer produced the historical exhausted-budget explanation.

## Limits

This is a read-only explanation of existing evidence. It does not repair a missing review report or change task execution.
An unrecognized blockage still requires an operator to read the original evidence in Advanced. Browser preference storage
is local to the browser, not a per-account server setting. The tests simulate failures and decisions; they are not human
usability testing with independent nontechnical participants.
