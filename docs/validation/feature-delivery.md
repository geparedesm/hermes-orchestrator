# Feature delivery backend validation

Status: deployed and validated on 2026-10-19 for the registered Odoo projects.

## Changes

- Migration 0021: role-specific model choices, task delivery settings and overrides, feature-plan artifacts,
  immutable shared preview candidates and a single-active-version constraint.
- Model resolution supports task overrides, pinned project defaults, role defaults, provider defaults and CLI defaults.
  Explicit execution selections retain precedence. Role changes require an allowed human approver.
- FEATURE planning requires a complete implementation assignment and eight nonempty plan sections. The document is
  stored durably and its hash is included in plan approval. Changed documents create a new plan version.
- Developer and reviewer context includes the original request, current requirements, plan and clarification answers.
  Corrections may resume the same developer session within the same task, subtask and workspace.
- Preview preparation and deployment attestation are operator-only API actions. Human-test tasks require a current
  preview; opening it is separate from the single explicit merge decision. The approval binds its manifest hash.
- A tested reverse proxy validates the operator with the installed Hermes `/api/auth/me` contract, strips authentication
  cookies and identity headers before forwarding to the test application, refuses cross-origin changes and hides
  database management routes. It is deployed as the authenticated gateway for the live preview environment.
- Dashboard role/task model selectors, structured plans and the human-test panel were implemented from the approved written layout.
- Human-test merge notifications include the test URL and explicitly describe acceptance as testing plus merge authorization.

## Verification

All checks ran in a disposable container using the existing throwaway `ho-test` PostgreSQL and Redis services.
Each integration test run created and migrated a separate random database and removed it afterwards.
After validation, migration 0021 and the backend/plugin were deployed. A separate preview stack and Cloudflare
subdomain were created. The original Odoo database was only read; its source application was not recreated.

- Final complete unit suite including Dashboard-origin and native-token protections: **367 passed**.
- Dashboard route tests including cross-origin rejection: **14 passed**.
- Orchestration, Git, approval-details, delivery and configuration regression selection: **106 passed**.
- Final focused selection after Dashboard compatibility and project-model additions: **49 passed**.
- Final delivery integration suite including grouped approval and mandatory human testing: **14 passed**.
- Ruff passed for changed Python modules, plugin backend, migration and added tests.
- `scripts/validate_schemas.py` passed schemas, valid/invalid examples and both machine profiles.
- `git diff --check` passed.

Counts describe separate runs with overlapping tests; they must not be added as a unique-test total.
The installed test dependencies reported a pre-existing Starlette/httpx deprecation warning and an existing Git test
helper returned a value that newer pytest warns about. Neither was a test failure.

## Deployment and browser evidence

- `https://test.gp-developer.com/web` returns 303 to login when unauthenticated.
- Hermes operator login returned 200; an automatic, separate preview Odoo session required no second password.
- Odoo health returned 200, the main navigation rendered, and the baseline version marker was visible.
- `/web/database/manager` returned 403. WebSocket opened through the authenticated gateway, and Odoo returned 101.
- Desktop (1400px) and mobile (390px) screenshots were captured. The browser reported no JavaScript page errors.
- Dashboard task creation displayed all six provider/role model selectors and accepted a planning-model selection. Saving the existing planning role through the authenticated public Dashboard returned 200.
- A browser-only task fixture confirmed one enabled preview button, visible human test instructions and a disabled merge approval before testing; no real task was modified or approved.
- Interactive login and Odoo rendering passed on desktop and mobile without JavaScript errors.
- The controller exported the registered default branches and successfully built a clean Odoo image; no candidate or merge was fabricated.
- Source Odoo `/web/health` still returned 200. The source database has no preview anonymization marker; the copy has v1.
- Boolean checks confirmed synthetic tenant/profile labels without printing any source personal data.
- The controller user service is enabled and active; user lingering is enabled. Preview containers use restart policies.
- The new policy is FEATURE + human-test-required; Claude planning is configured as opus.

## Limits and recovery

No ready live feature existed at deployment. Automatic source export/build is implemented and the freeze/approval
invariants were tested; the next actual feature is the live pilot for comparative quality, tokens and cost.
Current deployment support covers registered Odoo parent/module projects. Additional application families need an
adapter. Anonymization of a refreshed source copy refuses an unreviewed schema fingerprint or installed-module set.
Shared approval is transactional in PostgreSQL; merges across separate Git repositories are not one atomic Git operation.

The pre-migration dump is private under backups/feature-delivery. The original image reference was recorded, but Docker
no longer resolves that untagged image by its old digest; rebuilding the old source revision is required for that image.
The deployed image has an explicit feature-delivery-20261019 tag and its digest is recorded. No claim is made that the
original image remains locally available. Production Odoo remains separate and unchanged.

The operator explicitly approved the written layout in docs/design/feature-delivery.md when Pencil was unavailable.
No Pencil artifact or approval was fabricated.
