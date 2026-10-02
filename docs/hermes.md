# Using hermes-orchestrator through Hermes

Phase 9 makes official Hermes (`nousresearch/hermes-agent`, pinned by digest, unmodified) the primary interface. The `orchestration` plugin is a thin client of the orchestrator's Task API; PostgreSQL remains the only authority. Design: [docs/design/phase-9.md](design/phase-9.md).

## What you get

| Surface | What it does | Who can do what |
| --- | --- | --- |
| Chat: `/orch …` | `tasks`, `status T-n`, `create <project> <request>`, `approvals`, `approve <id>`, `reject <id> [note]`, `pause/resume/cancel/retry T-n`, `revise T-n <text>`, `budget T-n [counter=N]`, `projects` | Anyone Hermes lets talk to it can read and create; human actions use the sender's identity (`telegram:<user id>`); approving needs that identity in `HO_APPROVERS` |
| Chat: Hermes's agent | Tools `orch_task_create`, `orch_task_status`, `orch_task_list`, `orch_task_inspect`, `orch_project_list`, `orch_approvals_list` | Read and create only: the model can never approve, cancel, or change budgets (AD-10) |
| Dashboard: Orchestration tab | New tasks (**+ New task**: project, request, title, priority, budget, dependencies), pending approvals (approve/reject), and active tasks (pause/resume) | The operator behind the Dashboard login (`dashboard:operator`) |
| Host: `docker compose exec hermes hermes orchestration <verb>` | The same verbs as `/orch` | Principal `hermes-cli:<user>`; approvals still need an approver |
| Notifications | Approvals, logins needed, blocked tasks, budgets, definitive test failures, recovery problems, degraded platform, ready for merge, failed and completed tasks — immediately, with the command to answer. Progress — one digest every 5 minutes. | — |

`TASK_COMPLETED` is sent only when a task is `DONE`: after its merge was approved and the post-merge verification passed.

## Setup

1. `make secrets` creates `secrets/ho_hermes_dashboard_password` and `secrets/ho_hermes_webhook_secret` (with the other platform secrets).
2. `make up` starts `hermes` with the plugin enabled. The Dashboard listens on `http://127.0.0.1:9119` (`HO_HERMES_DASHBOARD_PORT`); log in as `operator` with the password in `secrets/ho_hermes_dashboard_password`.
3. Hermes needs a model provider for its own agent (not for `/orch`): configure it with Hermes's own tools, for example `docker compose exec hermes hermes model`.

## Connect a chat channel (Telegram example)

Add to `.env`, then `make up`:

```bash
TELEGRAM_BOT_TOKEN=<token from @BotFather>
TELEGRAM_ALLOWED_USERS=<your Telegram user id>            # who may talk to Hermes at all
HO_APPROVERS=telegram:<your Telegram user id>             # who may approve merges and budgets from chat
HO_HERMES_DELIVER=telegram                                 # where notifications go
HO_HERMES_DELIVER_CHAT_ID=<chat id that receives them>
HO_HERMES_WEBHOOK_URL=http://hermes:8644/webhooks/orchestration
```

`hermes-init` then enables Hermes's webhook platform with the route `orchestration` (`deliver_only`: the message is delivered without an agent turn), signed with `ho_hermes_webhook_secret` (HMAC V2, 5-minute replay window). Without `HO_HERMES_DELIVER` the route is not created and notifications wait in the outbox; `/orch` and the Dashboard still show everything, and the backlog is delivered once a channel is connected.

Other Hermes channels (Discord, Slack, Signal, …) work the same way with their own Hermes settings and `HO_HERMES_DELIVER=<platform>`.

## Check it

```bash
docker compose exec -T hermes hermes plugins list | grep orchestration     # enabled
docker compose exec -T control-plane ho recovery status                   # pending notifications, Hermes health
```

In the chat: `/orch tasks`, then `/orch status T-n`. A pending approval arrives as a message ending with `/orch approve <id>`.

## Security notes

- Hermes has no Docker socket, no project files, and no GitHub credentials; it reaches the control plane only through `ho-edge`, plus the Internet for its channels and model (`ho-hermes-egress`).
- The plugin's service token (`ho_plugin_token`) cannot run Git, review, or gate operations (operator-only routes refuse it); it forwards the human principal, and the control plane decides.
- The chat identity comes from Hermes's gateway after it authorized the sender (`TELEGRAM_ALLOWED_USERS`), bound to the message being handled; a model turn has no identity and cannot perform human actions.
- Notifications carry summaries and identifiers only — never secrets, diffs, or model output.
