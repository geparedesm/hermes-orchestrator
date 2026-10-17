# New project creation validation

Validated on 2026-10-18 (Australia/Perth), branch `codex/new-project`.

## Delivered behavior

Projects offers **New project**, with a name, description, and an optional GitHub repository checked by default.
The approved Pencil design is included in `ui/hermes-dashboard.pen`. A CREATE_PROJECT approval pins the template,
destination, and GitHub settings before publication. Git Service creates the repository, optionally pushes it,
and the control plane registers and scans it. A separate PROJECT_READY approval activates its configuration.

The generic template deliberately starts without required build, test, lint, or CI commands. The first task picks
the stack, creates passing tests, and proposes the real configuration for human approval. Protected branches,
independent review, blocking findings, and merge approval retain the platform's protections.

## Verification

| Check | Result |
| --- | --- |
| `make test-unit` | 355 passed |
| `make test-integration` | 269 passed, including existing Odoo child-project flows |
| Expanded `tests/integration/test_new_projects.py` | 8 passed (adds a transient-failure recovery test after the full suite started) |
| `tests/unit/test_git_projects.py` | 8 passed |
| `make lint validate-schemas` | Passed |
| `node --check hermes/plugins/orchestration/dashboard/dist/index.js` | Passed |
| `git diff --check` and Pencil JSON parsing | Passed |
| Chrome with a local Dashboard harness | Form renders; GitHub is checked initially and can be unchecked; filling the required fields enables submission |

The focused integration checks cover creation with and without GitHub, configuration activation from the shipped
template, independent renewal of two expired project requests, refusal to renew the same approval twice, reserved
names and folders, names retained by unregistered projects, rejection, unauthorized callers, and an unexpected
existing directory. Recovery after a simulated GitHub outage resumes from PUBLISHED and preserves local edits.

GitHub calls use `tests/fixtures/fake_gh.py` with local bare repositories; PostgreSQL and Redis are real throwaway
test services. No real user project or GitHub repository was created, and the operator's live stack was not deployed.

## Review follow-ups resolved

- Project creations have independent approval scopes, including expired-approval listing and renewal.
- Both the slug and destination are reserved against ordinary project registration.
- A slug retained by an unregistered project is refused before creating anything on disk or GitHub.
- The empty template explicitly disables inherited checks that have no commands yet.
- Running creations with transient errors expose Retry and Abandon in the Dashboard.
- Design documentation uses the implemented `/v1/project-creations` routes and describes the actual indexes.
