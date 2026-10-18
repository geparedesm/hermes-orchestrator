# Feature delivery and human acceptance

Status: deployed for the current Odoo parent/module projects on 2026-10-19; written layout explicitly approved by the operator. This document records the user's approved scope.

## Requirements

- All Hermes projects support a complete-feature delivery mode, retaining subtask mode for existing tasks.
- The operator selects planning, implementation and review models independently and may override them per task.
- Plans contain repository evidence, observable acceptance criteria, design, implementation steps, verification,
  human test instructions, risks and assumptions. One implementation assignment owns the complete feature.
- Shared previews group compatible projects (for example Odoo modules), freeze a version during human tests,
  and display the exact included tasks. Separate applications do not share a database or runtime accidentally.
- Public entry point: https://test.gp-developer.com. Only the operator may access it, from desktop or mobile.
- Odoo uses an anonymized snapshot of the current local installation, plus feature-specific sample data.
- One explicit human decision means “I tested this version and authorize the merge”. Opening the preview is
  recorded separately but never approves or merges anything.

## Backend contract implemented in this change

Migration 0021 adds role models, pinned task overrides and delivery mode, structured feature-plan artifacts,
plus durable preview candidates. Existing tasks retain their original workflow.

Model precedence: explicit execution model, per-task provider/role override, pinned project provider/role default, configured provider/role default,
provider default, CLI default. Already created execution assignments retain their selected model. The provider's
actual reported model and usage remain recorded by the existing usage system; no invented model prices are used.

FEATURE mode requires exactly one IMPLEMENT assignment, whose author writes its tests and handles corrections.
The independent review receives the original request, current requirements, structured plan and human answers.
A correction may resume the same developer's session only on the same task, subtask and workspace; authoritative
context is restored even if the session is unavailable. Existing review and failover limits still apply.

A preview candidate has PREPARING, READY, TESTING, RETIRED or FAILED lifecycle states. Its immutable manifest pins
included tasks, integration commits, tested bases, Quality Gate evaluations, configuration hashes and requirement
versions. A unique index prevents two active candidates for one environment. Replacing a candidate requires explicit
retirement; preparing another one cannot interrupt a test session silently. Candidates expire after seven days.
The deployment controller, not an agent or the Dashboard plugin, attests the prepared manifest is running.

Human-test tasks cannot obtain a merge approval without a current READY/TESTING candidate. The approval includes
the candidate identity and manifest hash. Consuming it additionally requires a started human test session and the
same candidate. Existing exact-commit, target-head and Quality Gate approval checks remain in place.

## Dashboard design for operator review

### New task (desktop and mobile)

Existing project, request and attachments fields remain first. An expandable “Delivery settings” section contains:

| Field | Presentation |
| --- | --- |
| Delivery | Complete feature (recommended); subtasks (advanced) |
| Human test | Required before merge; explanatory text about a preview being necessary |
| Models | Rows for Planning, Implementation and Review; provider-specific model selectors |
| Inheritance | “Use configured default” plus the effective choice; task choices do not change other tasks |

The provider is selected through routing as before; model choices are shown for each supported provider. No model
is described as better solely because of its price. Running assignments are never silently modified by selector changes.

### Agents / System settings

Each existing provider panel keeps its general model selector, then three rows: Planning, Implementation, Review.
Each has “Use provider default”, the existing model catalog, and an explicit Save action. Permissions remain human-only.

### Task detail: Test this version

A single full-width section above the final decision displays:

- Status: preparing / ready / you are testing / expired / unavailable.
- Shared environment and version; included task names; immutable candidate identifier.
- Primary action: Open test environment (records test start; never approves).
- What changed, required data, and a numbered human-test checklist from the feature plan.
- Request changes: a description sent as a requirement revision with the candidate reference.
- Final primary action: “I tested this version and authorize the merge”. Disabled until a current candidate is
  available and a human test session has started. Existing rejection remains available.

On mobile, selectors and actions stack vertically; included tasks use a compact list, not a wide table.
The preview itself shows a persistent version marker and returns to this task page for the final decision.

### Shared environment management

The operator can see included tasks and queued candidates. Refreshing the source database or replacing a candidate
is explicit and explains that current test edits will be replaced. New task completions queue instead of silently
updating the application currently under test.

Repository UI policy requires a Pencil design and explicit operator review before frontend changes. Pencil MCP
currently reports that the design file is not open/accessible, including when its absolute path is provided.
The operator explicitly approved using this written specification instead of a Pencil artifact on 2026-10-19.

## Deployed behavior and remaining validation

- `test.gp-developer.com` is served by an independent Cloudflare tunnel and the operator-authenticated gateway.
- The local Odoo source database was copied directly to a separate PostgreSQL instance, neutralized, anonymized and
  populated with synthetic profiles, rental objects, a contract and payments. Original uploaded documents were
  replaced by generated PDFs. Original Odoo remains on its own database and networks.
- One Hermes login provisions a preview-local Odoo session. Database management is blocked; HTTP mutations and
  WebSocket origins are checked. The visible version marker links back to Hermes.
- An operator-side persistent controller prepares pinned images for ready tasks, groups compatible repositories,
  installs/upgrades modules, runs reviewed synthetic-data hooks and activates only healthy candidates.
- A group approval lists every included task and consumes each member approval in one transaction. Git operations
  across repositories remain independent, so an interrupted group may need revalidation after a partial merge.
- New tasks default to FEATURE with human testing required; unfinished existing tasks also require human testing
  while retaining their existing plans. Claude planning defaults to `opus`; role/task selectors are available.
- Desktop/mobile browser checks, unit tests and integration tests with real Git repositories passed. No ready live
  feature existed at rollout, so comparative agent quality and token cost still need measurement on the next feature.

The installed adapter covers the registered Odoo parent and child-module repositories. Other application families
need an explicit runtime adapter and remain blocked from human-test merges until one is configured. Source-data
refreshes require the reviewed schema fingerprint; new production fields must be reviewed before another copy is published.

## Rollout and rollback

Human-test-required defaults were enabled after preparing the working preview and controller. Existing development
plans were retained. Migration 0021 was applied before starting code that uses it.
Keep database changes additive; rollback of code after migration is possible while old tasks retain their fields.
Do not remove candidate records while a merge is in flight. Preserve live Odoo and its database throughout rollout.
