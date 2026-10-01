#!/usr/bin/env bash
# Phase 9 end-to-end smoke test: official Hermes (pinned image, unmodified) with the orchestration plugin
# on a throwaway Compose stack. The plugin is enabled and its tools, /orch command, and CLI drive the real
# Task API; human actions need the message sender's identity (bound by Hermes's gateway) and an allowed
# approver; the Dashboard tab's API is behind Hermes's Dashboard login; notifications reach Hermes's webhook
# route signed with HMAC V2 (Hermes verifies them; a forged one is refused) and survive a Hermes outage.
#
# No chat account is used: the notification route targets Telegram without a bot, so Hermes accepts and
# verifies each delivery and then reports the channel as not connected. Delivery to a real chat is the
# operator check in docs/hermes.md.
#
# Usage: scripts/smoke-phase9.sh   (needs Docker, Internet for the Hermes image, ./secrets, and `make images`)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ho-smoke9
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ho-smoke9.XXXXXX")"
ROOT="$WORK/HermesProjects"
ENV_FILE="$WORK/smoke.env"
JAR="$WORK/cookies"
PORT=19119
APPROVER=4242
FAILURES=0

dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
ho() { dc exec -T control-plane ho "$@"; }
sql() { dc exec -T postgres psql -U ho_owner -d ho -tAc "$1"; }
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }
check() {
  if [[ "$2" == "$3" ]]; then echo "  ok    $1"; else echo "  FAIL  $1: expected '$2', got '$3'"; FAILURES=$((FAILURES + 1)); fi
}
contains() { [[ "$2" == *"$1"* ]] && echo True || echo False; }
wait_for() {
  local expected=$1 value=""
  shift
  for _ in $(seq 1 60); do
    value="$("$@" 2>/dev/null || true)"
    [[ "$value" == "$expected" ]] && break
    sleep 2
  done
  echo "$value"
}
probe() {  # probe <platform|none|tool> <user|tool name> <command or JSON> : output of the plugin inside Hermes
  dc exec -T -e HERMES_HOME=/opt/data hermes /opt/hermes/.venv/bin/python /opt/ho-tests/probe.py "$@" 2>/dev/null \
    | tail -1 | field 'd["output"]'
}
dash() { curl -s -b "$JAR" "$@"; }
cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

echo "== setup: Hermes with the orchestration plugin"
mkdir -p "$ROOT/app/.hermes"
printf 'version: 1\nproject: {name: app}\ntoolchain: {profiles: [generic]}\nagents: {allowed_providers: [claude, codex]}\n' \
  > "$ROOT/app/.hermes/project.yaml"
echo "x = 1" > "$ROOT/app/app.py"
git -C "$ROOT/app" init -q -b main && git -C "$ROOT/app" add -A \
  && git -C "$ROOT/app" -c user.name=u -c user.email=u@example.com commit -qm init
cat > "$ENV_FILE" <<ENV
HO_MACHINE_PROFILE=mac-m2-pro
HO_PROJECTS_ROOT_HOST=$ROOT
HO_VERSION=dev
HO_PROVIDER_IDENTITY=smoke9
HO_HERMES_DASHBOARD_PORT=$PORT
HO_HERMES_DELIVER=telegram
HO_HERMES_DELIVER_CHAT_ID=1
HO_HERMES_WEBHOOK_URL=http://hermes:8644/webhooks/orchestration
HO_APPROVERS=telegram:$APPROVER
ENV
dc up -d --build --wait >/dev/null
docker cp tests/hermes/probe.py "$(dc ps -q hermes)":/tmp/probe.py >/dev/null
dc exec -T -u 0 hermes sh -c 'mkdir -p /opt/ho-tests && cp /tmp/probe.py /opt/ho-tests/probe.py'
check "plugin enabled in Hermes" "True" \
  "$(contains enabled "$(dc exec -T -e HERMES_HOME=/opt/data hermes hermes plugins list 2>/dev/null | grep -i ' orchestration ')")"
check "native Kanban dispatch off (AD-02)" "false" \
  "$(dc exec -T -e HERMES_HOME=/opt/data hermes hermes config get kanban.dispatch_in_gateway 2>/dev/null | tr -d '[:space:]')"
ho project register "$ROOT/app" >/dev/null
ho project scan app >/dev/null

echo "== Dashboard tab behind Hermes's login"
check "plugin API refuses anonymous requests" "401" \
  "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/plugins/orchestration/overview")"
check "wrong password refused" "401" "$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
  -d '{"provider":"basic","username":"operator","password":"wrong"}' "http://127.0.0.1:$PORT/auth/password-login")"
LOGIN="$(python3 -c 'import json,sys; print(json.dumps({"provider": "basic", "username": "operator", "password": open(sys.argv[1]).read().strip()}))' secrets/ho_hermes_dashboard_password)"
check "operator login" "200" "$(curl -s -c "$JAR" -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
  -d "$LOGIN" "http://127.0.0.1:$PORT/auth/password-login")"
OVERVIEW="$(dash "http://127.0.0.1:$PORT/api/plugins/orchestration/overview")"
check "overview from the Task API" "app" "$(field 'd["projects"][0]["slug"]' <<<"$OVERVIEW")"
APPROVAL="$(field 'd["approvals"][0]["id"]' <<<"$OVERVIEW")"
check "approval decided from the Dashboard" "CONSUMED" "$(dash -H 'Content-Type: application/json' \
  -d '{"decision":"APPROVE"}' "http://127.0.0.1:$PORT/api/plugins/orchestration/approvals/$APPROVAL" | field 'd["state"]')"
check "project ready" "PROJECT_READY" "$(ho project show app | field 'd["status"]')"
check "decision recorded with the Dashboard principal" "dashboard:operator" "$(sql "SELECT decided_by FROM approvals WHERE id = '$APPROVAL'")"

echo "== LLM tools and /orch from chat"
TASK="$(probe tool orch_task_create '{"project": "app", "request": "Add a smoke feature"}' | field 'd["key"]')"
check "task created through the LLM tool" "T-1" "$TASK"
check "status answered from persistent state" "True" \
  "$(contains "$TASK [" "$(probe telegram "$APPROVER" "status $TASK")")"
check "human action without a sender identity refused" "True" "$(contains "Not allowed" "$(probe none x "pause $TASK")")"
check "pause by the approver from chat" "$TASK is now PAUSED." "$(probe telegram "$APPROVER" "pause $TASK")"
check "resume from chat" "True" "$(contains "is now" "$(probe telegram "$APPROVER" "resume $TASK")")"
check "pause recorded with the chat principal" "telegram:$APPROVER" \
  "$(sql "SELECT actor FROM events WHERE type = 'TASK_STATE_CHANGED' AND summary LIKE '%-> PAUSED%' LIMIT 1")"
mkdir -p "$ROOT/app2/.hermes" && cp "$ROOT/app/.hermes/project.yaml" "$ROOT/app2/.hermes/" && echo "y = 1" > "$ROOT/app2/app.py"
git -C "$ROOT/app2" init -q -b main && git -C "$ROOT/app2" add -A \
  && git -C "$ROOT/app2" -c user.name=u -c user.email=u@example.com commit -qm init
ho project register "$ROOT/app2" >/dev/null
SECOND="$(ho project scan app2 | field 'd["approval"]["id"]')"
check "a sender who is not an approver cannot approve" "True" \
  "$(contains "not an allowed approver" "$(probe telegram 999 "approve $SECOND")")"
check "the approver approves from chat" "True" "$(contains "PROJECT_READY" "$(probe telegram "$APPROVER" "approve $SECOND")")"
check "decision recorded with the chat principal" "telegram:$APPROVER" "$(sql "SELECT decided_by FROM approvals WHERE id = '$SECOND'")"

echo "== Hermes CLI"
check "hermes orchestration lists tasks" "True" \
  "$(contains "$TASK" "$(dc exec -T -e HERMES_HOME=/opt/data hermes hermes orchestration tasks 2>/dev/null)")"

echo "== notifications through Hermes's webhook"
sleep 8
check "deliveries verified by Hermes (no invalid signature)" "0" \
  "$(dc logs hermes 2>&1 | grep -c 'Invalid signature for route orchestration' || true)"
check "Hermes processed a direct delivery" "True" "$(contains "direct-deliver" "$(dc logs hermes 2>&1)")"
FORGED="$(dc exec -T control-plane python -c "
import urllib.request, time
req = urllib.request.Request('http://hermes:8644/webhooks/orchestration', data=b'{\"text\":\"x\"}', method='POST',
    headers={'Content-Type': 'application/json', 'X-Webhook-Timestamp': str(int(time.time())), 'X-Webhook-Signature-V2': '00' * 32})
try:
    urllib.request.urlopen(req, timeout=5); print(200)
except urllib.error.HTTPError as e:
    print(e.code)")"
check "forged notification refused by Hermes" "401" "$FORGED"
check "attention notifications wait for the channel, not lost" "PENDING" \
  "$(sql "SELECT state FROM notifications WHERE payload->>'type' = 'APPROVAL_REQUIRED' ORDER BY created_at LIMIT 1")"
check "the channel failure is recorded" "True" \
  "$(contains "502" "$(sql "SELECT last_error FROM notifications WHERE payload->>'type' = 'APPROVAL_REQUIRED' ORDER BY created_at LIMIT 1")")"
check "routine events do not interrupt (digest)" "0" \
  "$(sql "SELECT count(*) FROM notifications WHERE priority = 'ROUTINE' AND attempts > 0")"
check "internal events are not notified" "0" "$(sql "SELECT count(*) FROM notifications WHERE payload->>'type' IN ('GRANT_ISSUED', 'WORKER_CREATED', 'TASK_STATE_CHANGED')")"

echo "== Hermes outage"
dc stop hermes >/dev/null 2>&1
ho task cancel "$TASK" >/dev/null
check "work continues while Hermes is down" "CANCELLED" "$(ho task show "$TASK" | field 'd["state"]')"
dc start hermes >/dev/null 2>&1
check "Hermes back" "200" "$(wait_for 200 curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/auth/providers")"

echo
if [[ $FAILURES -eq 0 ]]; then echo "Phase 9 smoke: all checks passed"; else echo "Phase 9 smoke: $FAILURES check(s) failed"; exit 1; fi
