# Integration branch (`develop`): proposal

Status: proposal, not built. Motivation: T-53. The person approves every feature merge into `main`, and
features that wait for approval drift from `main` and conflict with it.

## Behavior

- **Opt-in per project.** Set `git.integration_branch: develop` in `.hermes/project.yaml`. Changing it is a configuration
  change, which a person approves. The branch must exist and must not be protected: not `main`, not `master`, not the
  default branch, and not in `git.protected_branches`. Otherwise the config is refused.
- **Feature tasks target `develop`.** Workspaces start from it, integration runs onto it, and verification and the
  Quality Gate run against it (`tasks.target_branch = develop`).
- **Automatic merge into `develop`.** After a passing Quality Gate, the control plane signs an `AUTO_MERGE`
  authorization: same signing key as MERGE, its own purpose, bound to the project, the target branch, the target SHA,
  the head SHA, and the evaluation id. No human approval is created. The Git Service verifies it like MERGE, and
  independently refuses it when the target is hard-protected or listed in `protected_branches` (read from the
  project's default branch, not from the task's head).
  - **Compare-and-swap on the server.** The Git Service builds the merge commit locally, with parents `target_sha` and
    the head. It moves `develop` only with a push that GitHub applies atomically against the expected old value:
    `git push origin <merge>:refs/heads/develop --force-with-lease=refs/heads/develop:<target_sha>`. A local project
    gets a local `update-ref` with the old value. `gh pr merge` is not used: it pins the head but not the base. If
    `develop` moved (a human push or another merge), the push is refused and the task reintegrates and is verified again
    before trying once more. Nothing that was not tested reaches `develop`.
  - If GitHub branch protection on `develop` refuses direct pushes, the task falls back to a human MERGE approval
    through a PR.
- **Task lifecycle.** `DONE` after post-merge verification on `develop`. The Dashboard shows "in develop, not released".
- **Release task (`develop` → `main`).** A new task kind `RELEASE`, created by the person from the Dashboard
  ("Release develop"). It has no developers. The final candidate is built first, then verified:
  1. Pin `main_sha` and `develop_sha`.
  2. Integrate both on a release branch (`hermes/release/<n>`): merge `develop_sha` onto `main_sha`. This catches
     human commits on `main` that the back-merge has not brought in yet.
  3. Optional version step: bump the Odoo manifests once (Y+1, Z=0) in a commit on that branch.
  4. Run verification, the full suite, and the Quality Gate on that final commit (`changes.integration_sha`).
  5. The usual human MERGE approval binds `main_sha` and that exact commit. Invariant 1 is unchanged.
  The release records the tasks it contains (merged into `develop_sha`). If `main` moves before the merge, the release
  rebuilds from step 1 and is verified again (the existing reintegrate-if-moved path).
- **Back-merge.** After a release, and whenever `main` moves (human commits), the platform merges `main` into
  `develop`: a fast-forward or merge commit through `AUTO_MERGE`. On conflict it creates a task for the person; nothing
  is overwritten (invariant 5).
- **People commit to `develop`.** Documented in each project's AGENTS.md and README; direct commits to `main` still
  work through the back-merge.

## Data and modules

- Migration `0016`: `tasks.kind` (`FEATURE` | `RELEASE`); `releases` (id, project, head_sha, merged task ids, version,
  state).
- Code:
  - `gitpolicy.sign_auto_merge` / `verify_auto_merge`;
  - `gitops.auto_merge` and `gitops.back_merge`;
  - Git Service `/v1/merge` takes the authorization kind;
  - `verification` and the Quality Gate take the task kind;
  - Dashboard: a Release button and an "unreleased" column.

## Tests

- Unit: `AUTO_MERGE` is refused for main, master, the default branch, a listed protected branch, a stale target SHA,
  or a reused authorization. A remote `develop` advanced between the check and the push makes the push fail, and
  nothing changes.
- Integration: a feature on a `develop` project merges into `develop` with no approval and reaches `DONE`; a release
  needs MERGE approval and verifies the final bumped candidate; a release rebuilds when `main` moves; a back-merge conflict creates a person task; parallel features no longer conflict on the
  version line.
- Smoke test on a throwaway stack.

## Risks

1. `AUTO_MERGE` becomes a bypass of invariant 1 if branch checks disagree. The Git Service re-checks protection on its
   own, from the default branch's config.
2. `develop` drifts from `main`. Back-merge on every move of `main`.
3. A broken `develop` blocks every feature. The Quality Gate runs a full suite before each auto-merge; a failing
   `develop` blocks auto-merges and notifies the person.
4. Concurrent auto-merges and human pushes race on `develop`. A server-side compare-and-swap (the lease push); the
   loser reintegrates and is verified again.

## Design review

Codex adversarial review (needs-attention), both findings applied:
- `gh pr merge` does not compare and swap the base. Replaced by a lease push that GitHub applies atomically.
- The release gated `develop` before the version bump and before integrating `main`. The final candidate is now
  built first and then verified.
