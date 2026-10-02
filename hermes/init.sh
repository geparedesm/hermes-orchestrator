#!/bin/sh
# One-shot Hermes setup for hermes-orchestrator (run by the `hermes-init` service, docs/design/phase-9.md).
# Uses only Hermes's own CLI on its data volume: enables the orchestration plugin, configures the webhook
# route the control plane delivers notifications to (HMAC V2, deliver_only), keeps native Kanban dispatch
# off (AD-02), and turns on the bundled basic Dashboard auth (a non-loopback bind requires a provider).
set -eu
export HERMES_HOME=/opt/data HOME=/opt/data
secret() { tr -d '\n' < "/run/secrets/$1"; }

hermes plugins enable orchestration >/dev/null
hermes config set kanban.dispatch_in_gateway false >/dev/null

# Notification route: Hermes delivers `deliver_only` messages only to a real channel, so the route exists
# only when one is configured (HO_HERMES_DELIVER=telegram|discord|slack|..., plus the channel's own
# settings, for example TELEGRAM_BOT_TOKEN and HO_HERMES_DELIVER_CHAT_ID). Without it, notifications wait
# in the orchestrator's outbox and remain visible through /orch and the Dashboard.
if [ -n "${HO_HERMES_DELIVER:-}" ]; then
  hermes config set platforms.webhook.enabled true >/dev/null
  hermes config set platforms.webhook.extra.port 8644 >/dev/null
  hermes config set platforms.webhook.extra.routes.orchestration.secret "$(secret ho_hermes_webhook_secret)" >/dev/null
  hermes config set platforms.webhook.extra.routes.orchestration.deliver_only true >/dev/null
  hermes config set platforms.webhook.extra.routes.orchestration.prompt "Orchestrator · {text}" >/dev/null  # not a bare {text}: values are parsed as YAML
  hermes config set platforms.webhook.extra.routes.orchestration.deliver "$HO_HERMES_DELIVER" >/dev/null
  if [ -n "${HO_HERMES_DELIVER_CHAT_ID:-}" ]; then
    hermes config set platforms.webhook.extra.routes.orchestration.deliver_extra.chat_id "$HO_HERMES_DELIVER_CHAT_ID" >/dev/null
  fi
  echo "hermes-init: notifications go to $HO_HERMES_DELIVER"
else
  hermes config set platforms.webhook.enabled false >/dev/null
  echo "hermes-init: no notification channel configured (HO_HERMES_DELIVER unset); notifications stay in the outbox"
fi

hermes config set dashboard.basic_auth.username operator >/dev/null
hermes config set dashboard.basic_auth.password "$(secret ho_hermes_dashboard_password)" >/dev/null
# Signs the Dashboard's login sessions. Its own secret: config.yaml is readable by Hermes, so no other secret
# may be derivable from this value.
hermes config set dashboard.basic_auth.secret "$(secret ho_hermes_dashboard_session_secret)" >/dev/null

chown -R 10000:10000 /opt/data 2>/dev/null || true
echo "hermes-init: orchestration plugin enabled"
