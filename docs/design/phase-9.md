# Phase 9 design: Hermes integration

Scope: PHASES.md Phase 9; ARCHITECTURE §7.1 (plugin contract), AD-01, AD-10, AD-12; DISCOVERY §3. Hermes stays unmodified: official image `nousresearch/hermes-agent` pinned by digest (`sha256:fca358f1…`, v0.21.5), one user plugin `orchestration`.

## Verified against the pinned image (source copied from the image, plus a live probe)

- Plugins live in `$HERMES_HOME/plugins/<name>/` (`plugin.yaml` + `__init__.py` with `register(ctx)`) and are activated with `hermes plugins enable <name>` (probe: listed as `user`, `enabled`; `hermes orchestration` ran our CLI handler).
- `ctx.register_tool(name, toolset, schema, handler)`; handler `(args: dict, **kw) -> str` (JSON). `ctx.register_command(name, handler)`; handler `(raw_args: str) -> str | None`. `ctx.register_cli_command(name, help, setup_fn, handler_fn)`.
- **Sender identity (OI-01, resolved):** the gateway authorizes the user for the source first, then runs plugin command handlers inside `_session_env_scope(build_session_context(source))`, so `gateway.session_context.get_session_env("HERMES_SESSION_PLATFORM"/"HERMES_SESSION_USER_ID")` is the authenticated sender of that message (`gateway/run_inbound.py`).
- **Notifications in:** webhook platform on port 8644, routes `POST /webhooks/<route>` under `platforms.webhook.extra.routes`, generic HMAC V2 (`X-Webhook-Signature-V2` = hex HMAC-SHA256 of `"<timestamp>.<body>"`, `X-Webhook-Timestamp`, 300 s replay window), `deliver_only: true` (the rendered prompt is the message, no agent turn), `deliver: log|telegram|…`, duplicates dropped by `X-Request-ID`.
- **Dashboard:** `plugins/<name>/dashboard/manifest.json` (`tab`, `entry`, `api`), backend `router` mounted at `/api/plugins/<name>/` behind the Dashboard auth middleware; frontend via `window.__HERMES_PLUGIN_SDK__`.

## Design

1. **Plugin** `hermes/plugins/orchestration` — a thin client of the Task API (stdlib HTTP, `HO_API_URL`, token `ho_plugin_token`, `Idempotency-Key` per call). Principal = `<platform>:<user id>` from the session for slash commands; `dashboard:operator` for the Dashboard; `hermes-cli:<user>` for the CLI. No task state in the plugin.
   - LLM tools (read and create only, AD-10): `orch_task_create`, `orch_task_status`, `orch_task_list`, `orch_task_inspect`, `orch_project_list`, `orch_approvals_list`.
   - Slash `/orch`: `tasks`, `status <T>`, `create <project> <request>`, `approvals`, `approve <id>`, `reject <id> [note]`, `pause|resume|cancel|retry <T>`, `revise <T> <text>`, `budget <T> [counter=N]`, `projects`. Human actions require a session identity; the control plane still checks `platform.approvers` for approvals.
   - CLI `hermes orchestration …` with the same verbs; Dashboard tab (task list, approvals with approve/reject; the full views are Phase 10).
2. **Control plane** — outbox delivery to `http://hermes:8644/webhooks/orchestration` signed with HMAC V2 (`ho_hermes_webhook_secret`): attention events (approval, auth, blocked, budget, definitive test failure, recovery failure, degraded platform, merge readiness, task failed, completion) one message each, immediately; curated routine events aggregated into one digest every 5 minutes; internal events (grants, workers, routing) are not notified. `TASK_COMPLETED` is emitted only on `DONE`, which requires the approved merge and passing post-merge verification (Phase 5). Messages carry the action to take (`/orch approve <id>`).
3. **Compose** — `hermes` service (pinned digest, container-native `hermes-data` volume, plugin mounted read-only, gateway + webhook platform) and a one-shot `hermes-init` that enables the plugin and writes the webhook route; Hermes reaches only the control plane (no Docker socket, no project files).

## Tests

Unit: message rendering, digest grouping, HMAC V2 signature (against Hermes's own formula), plugin argument parsing and principal resolution (session env set through Hermes's `set_session_vars` in the real image). Integration: outbox with a mock Hermes (attention immediate, routine digest, internal events silent, order, signature). Smoke (`smoke-phase9.sh`, real Hermes container): plugin enabled; tools and `/orch` handler drive the real Task API (create, status, approve with an authorized principal, refusal for an unauthorized one); CLI; Dashboard plugin API rejects unauthenticated requests; a task event reaches Hermes's webhook (`delivered`, signature verified by Hermes) and an outage is caught up.
