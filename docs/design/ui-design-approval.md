# UI design before frontend implementation

Hermes creates the UI proposal itself with the official headless pen.dev CLI. A running VS Code extension or desktop app is unnecessary. Existing encrypted `.pen` files are opened only through pen.dev, never parsed as text. The read view includes tracked project files only; a referenced local/ignored design must be attached to the task or tracked in the repository. Missing references block the design instead of silently replacing it.

## Workflow

1. Plan the task. Frontend language in the original request/requirements, UI paths in the plan, an explicit `ui_design_required: true` creation flag, or `REQUEST_UI_DESIGN` require visual approval. Detection is conservative and the orchestrator must explicitly request design for UI work whose wording/paths are ambiguous.
2. `REQUEST_UI_DESIGN` launches a metered Claude/Codex worker with project files mounted read-only, no implementation workspace or Git write capability, and only the project's local/test `PEN_CLI_KEY` secret. It calls `ho-pen interactive` to create/edit the design; it does not start a nested pen.dev AI agent.
3. Collect `design/design.pen`, `design/desktop.png`, `design/mobile.png`, `design/design.md` from `/output`. Missing/invalid exports block the task. Save a durable copy under `ui-designs/`, outside execution-output retention.
4. Request `UI_DESIGN` approval and move the task to `APPROVAL_REQUIRED`. The approval binds the manifest hash and the requirements/plan versions. Images must be delivered or an approver must explicitly attest review of the authenticated downloads before approval can be consumed.
5. Human rejection records feedback and returns to planning. The next proposal includes the previous design package and feedback. New requirements or a new plan require a new design approval.
6. Implementation and cross-review receive the approved images, specification and encrypted source read-only. Creation and dispatch both check the current approval and package hashes. Reviewers are instructed to compare the implemented UI against the approved exports.

This is a server-enforced prerequisite for detected/declared UI work, including manual execution requests. It does not depend on a model obeying a sentence in its prompt. As with other scope estimates, path/word detection cannot infer every possible effect of arbitrary code; the planner must declare UI impact even when heuristics do not detect it.

## Setup

The CLI is pinned in `workers/versions.env` and installed in both provider worker images. Rebuild the workers and apply migration 0022 before enabling the new code. No runtime provider login or live project is changed by installing this feature.

Create a CLI key in pen.dev organization settings → Developer Keys. Never paste the key into a task, chat, repository, or command argument. Use the existing Secrets Broker to place it in:

```
project-secrets/<project-slug>/local/PEN_CLI_KEY
```

Use mode 0600 and the same ownership/access rules as other project secrets. Declare its reference in the project's approved `.hermes/project.yaml`:

```yaml
secrets:
  - name: PEN_CLI_KEY
    environment: local
    delivery: file
```

Approve the resulting configuration proposal through the normal onboarding flow. Project network policy must permit `api.pen.dev` and any pen.dev asset hosts actually used by the design. No credentials are copied from the user's VS Code configuration. Workers with missing/expired pen.dev authentication return BLOCKED instead of implementing frontend code.

The official CLI reference is <https://docs.pen.dev/for-developers/pen-cli>. `ho-pen` permits tool-shell/help/version commands and refuses nested AI-agent mode. Existing Claude/Codex subscriptions continue to drive the outer, budgeted worker.

## Inspection and decisions

- `ho task design T-8`: current design version, manifest and state.
- Authenticated `GET /v1/tasks/T-8/ui-design/<design-id>/desktop.png` and `mobile.png`: exact exports; no public file URLs.
- For an operator who has downloaded and reviewed both images, `ho task design-reviewed T-8 <design-id>` records that attestation. It does not approve the design.
- Approve or reject the numbered `UI_DESIGN` decision with the existing `/orch approve`, `/orch reject <number> <feedback>` or short replies.

The image-delivery API only records delivery receipts; it never grants approval. The normal human principal allowlist remains authoritative for the decision. The plugin's image delivery must be bound to the explicitly authorized notification destination.

## Explicit image-delivery opt-in

Image sending is disabled by default. After the owner authorizes the destination, set all three values and recreate the Hermes service:

```dotenv
HO_UI_DESIGN_DELIVERY=true
HO_UI_DESIGN_DELIVER_PLATFORM=telegram
HO_UI_DESIGN_DELIVER_CHAT_ID=<explicitly-authorized-chat-id>
```

The plugin never inherits a general home/notification destination and never accepts a destination from a task or model. With missing or false activation, it registers no sender. With activation but a missing platform/chat ID, it also registers no sender. Images are downloaded over the internal authenticated API, checked against the package hash and sent with the adapter's native image method; a failed upload is retried without recording a receipt. This helper cannot approve designs. Disabling the flag stops delivery on the next poll.

The designer enables `NODE_USE_ENV_PROXY=1` so pen.dev and its font downloads use the mandatory worker proxy. Native `Export` creates a directory with `<frame-id>.png`; the designer copies each frame into the exact desktop/mobile package filenames and checks regular files before reporting success.
