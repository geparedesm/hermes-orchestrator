# Phase 9 Validation: Hermes Integration

Branch `codex/phase-9-hermes`. Design: [docs/design/phase-9.md](../design/phase-9.md). Operator guide: [docs/hermes.md](../hermes.md).

## What was built

- **Hermes service**: the official image `nousresearch/hermes-agent@sha256:fca358f1…` (v0.21.5), unmodified, in Compose with a container-native `hermes-data` volume, the Dashboard on `127.0.0.1:9119` behind Hermes's bundled basic login, and networks `ho-edge` (control plane only) and `ho-hermes-egress` (its channels and model). `hermes-init` uses only Hermes's CLI to enable the plugin, keep native Kanban dispatch off (AD-02), configure the Dashboard login, and create the notification route when a channel is configured.
- **Plugin `orchestration`** (`hermes/plugins/orchestration`): six read-and-create LLM tools, the `/orch` command for human actions, `hermes orchestration …` on the CLI, and a Dashboard tab (approvals, active tasks) whose backend proxies the Task API. It stores no state; it forwards the principal (`<platform>:<user>`, `dashboard:operator`, `hermes-cli:<user>`) and the control plane decides.
- **Notifications** (`control_plane/notifications.py`): outbox delivery to Hermes's webhook route `orchestration` (`deliver_only`), signed with Hermes's generic HMAC V2. Attention events go out one by one with the command to answer (`/orch approve <id>`); curated routine events go out as one digest at most every 5 minutes; internal events (grants, workers, routing, steps) are not notified. `TASK_COMPLETED` exists only for `DONE` (approved merge plus passing post-merge verification).
- **Approvers**: `dashboard:operator` by default; chat identities with `HO_APPROVERS` (for example `telegram:<id>`).
- **Task API**: `GET /v1/tasks?active=true` filters before the limit.

## Verified against the pinned Hermes release

Source copied out of the pinned image and exercised in the running container:

| Question | Answer | Evidence |
| --- | --- | --- |
| Plugin contract | `plugin.yaml` + `register(ctx)`; `register_tool(name, toolset, schema, handler)`, `register_command(name, handler)`, `register_cli_command(name, help, setup_fn, handler_fn)` | `hermes_cli/plugins.py`; `hermes plugins list` shows `orchestration` enabled |
| Sender identity for slash commands (OI-01) | Bound by the gateway after authorizing the sender, through `gateway.session_context` | `gateway/run_inbound.py`; `tests/hermes/probe.py` |
| Dashboard plugin API authentication (OI-04, D08) | 401 anonymous, 401 wrong password, 200 after login | smoke |
| Webhook delivery | HMAC V2 accepted; forged signature 401; `deliver_only` requires a real channel; failed deliveries' request IDs are cached as duplicates | smoke; `gateway/platforms/webhook.py` |

## Evidence

| Check | Result |
| --- | --- |
| `make lint`, `make validate-schemas` | pass |
| `make test-unit` | 229 passed (12 for the plugin) |
| `make test-integration` | 129 passed (8 in `test_notifications.py`) |
| `scripts/smoke-phase9.sh` | all checks pass with the real Hermes container |
| `scripts/smoke-phase2.sh` … `smoke-phase8.sh` | all pass (Hermes is now part of every stack) |

The Phase 9 smoke drives the real Hermes: plugin enabled, Kanban dispatch off; Dashboard login and the tab's API (overview from the Task API, an approval decided as `dashboard:operator`); a task created through the LLM tool (Hermes's tool registry), its status answered from persistent state; `/orch` with a bound sender (pause and resume recorded as `telegram:4242`), refused without a sender, refused for a non-approver, accepted for the approver; `hermes orchestration tasks`; notifications verified by Hermes (no invalid signature), a forged one refused, attention notifications kept pending while the channel is not connected (no chat account in the test), no routine or internal notifications sent early; work continuing while Hermes is stopped.

## Defects found while integrating with the real Hermes

1. `hermes config set` parses values as YAML: a bare `{text}` template became a mapping and Hermes failed rendering it (500). The route template is now `Orchestrator · {text}`.
2. Hermes caches a webhook's `X-Request-ID` even when the delivery fails and answers a retry as a `duplicate` with 200, which would have lost the message. Request IDs now carry the attempt number (delivery is at least once).
3. Hermes refuses a `deliver_only` route whose target is `log`; the route is now created only for a real channel, and notifications wait in the outbox otherwise.
4. The stale control-plane image in the first probe (built by `make images`, which builds only workers) showed the old bearer-token outbox; smoke tests build with `--build`.

## Codex review (`/codex:review --base main`)

Three findings, all fixed: active tasks were filtered after the list limit (old active tasks could be hidden); a mutation without an answer was reported as not applied (now "may have been applied, check before repeating"); routine digests were not rate-limited for large backlogs (now one digest per 5 minutes, summarized).

## Decisions made in this phase

- Human actions from chat use the identity Hermes's gateway bound to the message; model turns have none and cannot act for a human.
- The Dashboard acts as `dashboard:operator`, protected by Hermes's own login.
- No notification channel is assumed: without one, notifications wait in the outbox and the state stays visible through `/orch` and the Dashboard.

## Known limitations

- Delivery to a real chat was not exercised (no chat account in the test environment); the operator check is in `docs/hermes.md`.
- Hermes's own agent needs a model provider configured by the operator; the tools were exercised through Hermes's tool registry, not through a model turn.
- The Dashboard tab is minimal (approvals, active tasks); the full views are Phase 10.

## Operator review

- [ ] Connect a channel and receive a notification (docs/hermes.md).
- [x] Merged under the operator's standing authorization (2026-10-01).
