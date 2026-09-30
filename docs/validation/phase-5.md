# Phase 5 Validation: Git Isolation

**Date:** 2026-09-30  
**Machine:** MacBook Pro (Apple Silicon), Docker Desktop 29.4.0, `linux/arm64`, Docker VM with 16 GB RAM  
**Scope:** PHASES.md Phase 5 and its completion criteria: *work stays isolated, human changes are not silently overwritten, and protected merges cannot proceed without valid human approval.*

## What was built

| Deliverable | Location |
| --- | --- |
| Git rules shared by both services: protected branches, platform branch names, signed merge authorizations, human change classification | `packages/ho_core/src/ho_core/gitpolicy.py` |
| Git Service: isolated workspaces, hardened collection, divergence, integration in the object database, conflict workspaces, approved local merges | `services/git-service/.../repo_ops.py`, `app.py` |
| Git Service GitHub operations: push with lease, PR create/update/view, CI checks, approved PR merge, branch deletion | `services/git-service/.../github.py` (official `gh` 2.101.0, pinned by checksum) |
| Control plane: workspaces per task, human change monitor, integration with retest, agent-assisted conflict resolution, MERGE approvals, merge execution, post-merge verification | `services/control-plane/.../gitops.py`, migration `0004` |
| Operator CLI: `ho git workspace|status|collect|divergence|integrate|resolve|push|pr|checks|merge-request` | `services/control-plane/.../cli.py` |
| GitHub login for Git Service only | `make auth-github` (`gh-config` volume) |
| Compose: git-service is the only read-write mount of the projects root; `ho-git-egress`; `ho_merge_key` secret | `compose.yaml`, `scripts/init-secrets.sh` |

## Evidence

| Check | Command | Result |
| --- | --- | --- |
| Unit tests (adds Git rules and Git Service against real repositories) | `make test-unit` | 173 passed |
| Integration tests (adds 15 control-plane Git tests with the real Git Service in process) | `make test-integration` | 63 passed |
| Agent Manager and real CLIs against Docker (regression) | `make test-docker` | 39 passed |
| Phase 5 end-to-end on the Compose stack | `make smoke-phase5` | 23 of 23 checks passed |
| Phase 4, 3, 2 end-to-end (regression) | `make smoke-phase4`, `smoke-phase3`, `smoke` | 29/29, 21/21, 21/21 |
| Lint, schemas | `make lint validate-schemas` | clean |

### Work stays isolated

- Each workspace is a clone created from one ref: no remote, none of the user's branches, none of the other tasks' refs. The first workspace pins the task's base (`refs/hermes/tasks/<task>/base`), and later workspaces start from it even after `main` moves.
- An execution can mount only an active workspace registered to its own task; another task's workspace or an unregistered path is rejected (integration test).
- In the smoke test a worker committed in its clone, had no remote to push to, and the user's checkout stayed unchanged (`git status` clean, same `HEAD`).
- Collection never runs anything inside a clone. Tests plant `core.fsmonitor`, a hooks directory, `uploadpack.packObjectsHook`, and `core.alternateRefsCommand` in a clone; none runs. Clones that redirect Git to another repository (gitfile, alternates, linked objects) are refused.

### Human changes are not silently overwritten

- Classification (§37) of what the user changed on the target since the task's base against what the task changed, including uncommitted work when the target is checked out: `LOW` different files, `MEDIUM` same file far apart, `HIGH` the same lines (within 3 lines) or both adding the same file, `CRITICAL` a deletion or rename against a change, or overlap in a configured critical path. Sensitive paths raise the level to at least `HIGH`.
- Actions: every level above `NONE` records `HUMAN_CHANGE_DETECTED`; `MEDIUM` and above mark reconciliation required; `CRITICAL` moves the task to `APPROVAL_REQUIRED` (approve to continue, reject to block). A scheduler pass re-checks tasks when their target branch moves.
- Integration runs in the object database: the user's branch and working tree are untouched, and the user's commits are kept in the result. A conflict changes nothing and is reported; `ho git resolve` prepares a clone with the conflict in place and asks an agent to resolve it, keeping the human change's intent or reporting blocked.
- After integration the result is retested in a fresh clone by a test runner (`commands.test`); merge requests are refused while that retest runs or has failed.
- A local merge never overwrites uncommitted work: with the target checked out, Git Service fast-forwards and Git refuses when local changes would be overwritten (the task becomes `BLOCKED`, the user's file is unchanged); unrelated uncommitted files stay as they were.

### Protected merges need a valid human approval

- The only way to merge is a `MERGE` approval requested from `READY_FOR_MERGE`, bound to the target branch, the exact target commit, the exact integrated (or PR head) commit, the method, and the PR number.
- On approval the control plane re-reads the state and consumes the approval only if the hash still matches; otherwise the approval is invalidated and the task returns to `RUNNING` for reintegration (smoke test: the user committed after the request; nothing was merged). The monitor also withdraws a pending request when the target moves.
- Git Service verifies the signed merge authorization itself and re-checks the commits before writing. A forged authorization was refused with 403 on the Compose stack; unit tests cover tampered subjects, a wrong key, expiry, another project, a moved target, and replays (idempotent result for the same approval).
- After the merge the task goes to `VERIFYING`; post-merge tests run on a fresh clone of the merged commit, and only their success makes the task `DONE` (`TASK_COMPLETED`). Failure makes it `BLOCKED`. Workspaces are removed after a successful merge.
- Only allowlisted approvers decide (a Hermes-channel principal was refused), and a rejected merge returns the task to `FIX_REQUIRED`.

### GitHub rules (bare remote and `gh` stand-in)

- Push only branches under the project prefix; `main`, `master`, the default branch, the project's protected list, other branch names, and option-like names are refused (403). No `--force`: updates use `--force-with-lease` bound to the last pushed commit, and a branch moved by someone else is not overwritten.
- Pull requests are created or updated (one per task branch); CI checks are summarized as `PASS`, `FAIL`, `PENDING`, or `NONE`.
- The approved PR merge checks the PR head, base branch, and remote base commit, and calls `gh pr merge --match-head-commit` (never `--admin` or `--auto`); a changed head is refused. Deleting protected branches is refused.
- Credentials: the GitHub token exists only in the `gh-config` volume of git-service; remote URLs with embedded credentials are sanitized in responses.

### OI-06: cost of isolated clones

A synthetic repository with 72 MB of history (3,000 files, 300 commits) clones into a workspace in 3.4 s using 121 MB; a collection with no new commits takes 0.12 s. `--reference` would make the clone depend on the main object store, which workers cannot see, so it was not adopted.

## Decisions made in this phase

| Decision | Reason |
| --- | --- |
| Integration and merges are computed with `git merge-tree` and `commit-tree`, with no working tree | No repository content, hooks, or filters run during integration; conflicts leave no half-merged state. |
| Merge authorization signed by the control plane and verified by Git Service | The merge rule is enforced twice, by different services; Git Service needs no database access. |
| A merge into the checked-out branch fast-forwards the user's checkout; otherwise the ref moves with compare-and-swap | Keeps the user's working tree consistent with the branch while Git itself refuses to overwrite local changes. |
| Local merges support `merge` and `squash`; `rebase` goes through GitHub pull requests | The pinned Git 2.39 (Debian bookworm) has no `--merge-base` or `git replay` for replaying commits without a working tree. |
| `.hermes/worktrees/` added to `.git/info/exclude` when needed | Keeps the user's `git status` clean without changing tracked files; the onboarding proposal still suggests the `.gitignore` entry. |
| Official `gh` release pinned by version and SHA-256 | Debian's packaged `gh` (2.23) predates the flags used. |
| Workspaces and branches named `<task>-<suffix>` and `<prefix><task>/<suffix>` | Unique per project, readable, and always under the platform prefix. |

## Known limitations

- **GitHub against the real service is not yet run.** Push, PR, checks, and PR merge were verified with a local bare remote and a `gh` stand-in; a run with a real repository needs `make auth-github` (operator review).
- `READY_FOR_MERGE` is reached by the Quality Gate in Phase 7; the tests and smoke test set it directly as a stand-in.
- The monitor re-checks when the target branch moves; uncommitted edits alone are seen when a divergence check runs (`ho git divergence`, integration, merge request), not continuously.
- Agent-assisted conflict resolution is started by the operator (`ho git resolve`); the orchestrator automates it in Phase 7. A failed resolution is escalated by blocking the task.
- `HIGH` changes mark reconciliation required but do not yet trigger replanning; that is the orchestrator's job (Phase 7).
- `ho-git-egress` is a plain bridge network for git-service; restricting it to GitHub hosts is Phase 11 hardening.
- Linux: git-service (UID 10001) needs write access to the projects root and to repositories created by the user; not yet run on Linux.

## Operator review

1. `make up` (rebuilds git-service with `gh` and applies migration `0004`).
2. Optional for GitHub: `make auth-github`, then on a throwaway GitHub repository cloned under the projects root: `ho git workspace`, an agent run, `ho git integrate`, `ho git push`, `ho git pr`, and a merge request approved in the platform.
