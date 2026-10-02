#!/usr/bin/env bash
# Phase 10 end-to-end smoke test: the orchestration views inside the real Hermes Dashboard.
# A throwaway stack gets real data (projects, a task with an execution, a workspace change verified by the
# Test Runner, a pending approval); the tab's API routes return the overview, board, task detail, workers,
# projects, and approvals behind Hermes's login; `/metrics` serves Prometheus text to the operator only; and
# headless Chromium (the Browser Runner image) renders the tab, walks its views, opens the task, and approves
# the pending approval from the page.
#
# Usage: scripts/smoke-phase10.sh   (needs Docker, ./secrets, and `make images`)
#        SCREENSHOT=docs/validation/phase-10-dashboard.png scripts/smoke-phase10.sh   to keep the overview screenshot
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ho-smoke10
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ho-smoke10.XXXXXX")"
ROOT="$WORK/HermesProjects"
ENV_FILE="$WORK/smoke.env"
JAR="$WORK/cookies"
PORT=19120
FAILURES=0
export GIT_CONFIG_GLOBAL=/dev/null GIT_AUTHOR_NAME=User GIT_AUTHOR_EMAIL=user@example.com \
       GIT_COMMITTER_NAME=User GIT_COMMITTER_EMAIL=user@example.com

dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
ho() { dc exec -T control-plane ho "$@"; }
sql() { dc exec -T postgres psql -U ho_owner -d ho -tAc "$1"; }
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }
check() {
  if [[ "$2" == "$3" ]]; then echo "  ok    $1"; else echo "  FAIL  $1: expected '$2', got '$3'"; FAILURES=$((FAILURES + 1)); fi
}
wait_for() {
  local expected=$1 value=""
  shift
  for _ in $(seq 1 90); do
    value="$("$@" 2>/dev/null || true)"
    [[ "$value" == "$expected" ]] && break
    sleep 2
  done
  echo "$value"
}
dash() { curl -s -b "$JAR" "http://127.0.0.1:$PORT/api/plugins/orchestration$1"; }
new_project() {  # new_project <name>
  mkdir -p "$ROOT/$1/.hermes"
  printf 'version: 1\nproject: {name: %s}\ntoolchain: {profiles: [generic]}\nagents: {allowed_providers: [claude, codex]}\ncommands: {test: "true"}\n' \
    "$1" > "$ROOT/$1/.hermes/project.yaml"
  echo "x = 1" > "$ROOT/$1/app.py"
  git -C "$ROOT/$1" init -q -b main && git -C "$ROOT/$1" add -A && git -C "$ROOT/$1" commit -qm init
  ho project register "$ROOT/$1" >/dev/null
}
cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

echo "== setup: real data on a throwaway stack"
mkdir -p "$ROOT"
printf 'HO_MACHINE_PROFILE=mac-m2-pro\nHO_PROJECTS_ROOT_HOST=%s\nHO_VERSION=dev\nHO_PROVIDER_IDENTITY=smoke10\nHO_HERMES_DASHBOARD_PORT=%s\n' \
  "$ROOT" "$PORT" > "$ENV_FILE"
dc up -d --build --wait >/dev/null
new_project app
ho approval approve "$(ho project scan app | field 'd["approval"]["id"]')" >/dev/null
new_project app2
ho project scan app2 >/dev/null  # left pending: approved from the page later
TASK="$(ho task create app "Add a dashboard feature" | field 'd["key"]')"
wait_for READY sh -c "docker compose -p $PROJECT --env-file $ENV_FILE exec -T control-plane ho task show $TASK | python3 -c \"import json,sys; print(json.load(sys.stdin)['state'])\"" >/dev/null
EXEC="$(ho execution run "$TASK" 'echo hello' | field 'd["id"]')"
wait_for SUCCEEDED sh -c "docker compose -p $PROJECT --env-file $ENV_FILE exec -T control-plane ho execution show $EXEC | python3 -c \"import json,sys; print(json.load(sys.stdin)['state'])\"" >/dev/null
WS="$(ho git workspace "$TASK" | field 'd["path"]')"
echo "y = 2" > "$ROOT/app/$WS/feature.py" && git -C "$ROOT/app/$WS" add -A && git -C "$ROOT/app/$WS" commit -qm "Add feature"
ho git integrate "$TASK" >/dev/null
check "verification by the Test Runner" "PASSED" \
  "$(wait_for PASSED sh -c "docker compose -p $PROJECT --env-file $ENV_FILE exec -T control-plane ho tests show $TASK | python3 -c \"import json,sys; d=json.load(sys.stdin); print(d['verifications'][0]['state'] if d['verifications'] else 'NONE')\"")"

echo "== tab API behind Hermes's login"
LOGIN="$(python3 -c 'import json,sys; print(json.dumps({"provider": "basic", "username": "operator", "password": open(sys.argv[1]).read().strip()}))' secrets/ho_hermes_dashboard_password)"
check "anonymous request refused" "401" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/plugins/orchestration/summary")"
check "operator login" "200" "$(curl -s -c "$JAR" -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "$LOGIN" \
  "http://127.0.0.1:$PORT/auth/password-login")"
SUMMARY="$(dash /summary)"
check "overview: required actions" "1" "$(field 'len(d["required_actions"]["approvals"])' <<<"$SUMMARY")"
check "overview: recent tests" "PASSED" "$(field 'd["recent_tests"][0]["state"]' <<<"$SUMMARY")"
check "overview: tasks by state" "True" "$(field 'sum(d["tasks_by_state"].values()) == 1' <<<"$SUMMARY")"
check "board lists the active task" "$TASK" "$(dash /board | field 'd["tasks"][0]["key"]')"
VIEW="$(dash "/tasks/$TASK")"
check "task view: executions" "True" "$(field 'len(d["executions"]) >= 2 and all(e["state"] == "SUCCEEDED" for e in d["executions"])' <<<"$VIEW")"
check "task view: test runs" "PASSED" "$(field 'd["tests"][0]["state"]' <<<"$VIEW")"
check "task view: audit timeline" "True" "$(field '"TASK_CREATED" in [e["type"] for e in d["timeline"]]' <<<"$VIEW")"
check "task view: checkpoints" "True" "$(field 'len(d["checkpoints"]) > 0' <<<"$VIEW")"
check "task view: Git state" "True" "$(field 'bool(d["git"]["integration_sha"])' <<<"$VIEW")"
check "workers view" "True" "$(dash /workers | field '"capacity" in d and "running" in d')"
check "projects view" "app,app2" "$(dash /projects | field '",".join(sorted(p["slug"] for p in d["projects"]))')"

echo "== metrics for future Prometheus scraping"
METRICS="$(dc exec -T control-plane python -c "
import urllib.request
token = open('/run/secrets/ho_operator_token').read().strip()
req = urllib.request.Request('http://127.0.0.1:8080/metrics', headers={'Authorization': 'Bearer ' + token})
print(urllib.request.urlopen(req).read().decode())")"
check "Prometheus text: tasks" "True" "$([[ "$METRICS" == *'# TYPE ho_tasks gauge'* && "$METRICS" == *'ho_tasks{state="READY"} 1'* ]] && echo True || echo False)"
check "Prometheus text: components" "True" "$([[ "$METRICS" == *'ho_component_healthy{component="agent_manager"} 1'* ]] && echo True || echo False)"
PLUGIN_METRICS="$(dc exec -T control-plane python -c "
import urllib.request, urllib.error
token = open('/run/secrets/ho_plugin_token').read().strip()
req = urllib.request.Request('http://127.0.0.1:8080/metrics', headers={'Authorization': 'Bearer ' + token, 'X-HO-Principal': 'x:y'})
try:
    urllib.request.urlopen(req); print(200)
except urllib.error.HTTPError as e:
    print(e.code)")"
check "metrics are operator-only" "403" "$PLUGIN_METRICS"

echo "== the tab rendered in headless Chromium"
mkdir -p "$WORK/out"
cp secrets/ho_hermes_dashboard_password "$WORK/out/password" && chmod 644 "$WORK/out/password"
SEEN="$(docker run --rm --network "${PROJECT}_ho-edge" -v "$PWD/tests/hermes:/check:ro" -v "$WORK/out:/out" \
  --entrypoint python3 hermes-orchestrator/browser-runner:"${HO_VERSION:-dev}" /check/dashboard_check.py \
  http://hermes:9119 /out/password "$TASK" /out/overview.png 2>&1 | tail -1)"
check "page login" "200" "$(field 'd["login"]' <<<"$SEEN")"
check "overview rendered" "True" "$(field 'd["overview"]' <<<"$SEEN")"
check "tab listed in Hermes's sidebar" "True" "$(field 'd["sidebar"]' <<<"$SEEN")"
check "board rendered with the task" "True" "$(field 'd["board"]' <<<"$SEEN")"
check "task detail sections" "10" "$(field 'len(d["task_sections"])' <<<"$SEEN")"
check "approval decided from the page" "1 0" "$(field 'str(d["pending_in_page"]) + " " + str(d.get("pending_after_approve"))' <<<"$SEEN")"
check "decision recorded as the Dashboard operator" "dashboard:operator" \
  "$(sql "SELECT decided_by FROM approvals WHERE state IN ('APPROVED', 'CONSUMED') ORDER BY decided_at DESC LIMIT 1")"
check "task created from the form" "True" "$(field 'd.get("created_from_form")' <<<"$SEEN")"
check "one task per form, created by the Dashboard operator" "1 dashboard:operator" \
  "$(sql "SELECT count(*) || ' ' || min(requested_by) FROM tasks WHERE title = 'Version endpoint'")"
if [[ -n "${SCREENSHOT:-}" && -f "$WORK/out/overview.png" ]]; then cp "$WORK/out/overview.png" "$SCREENSHOT"; fi

echo
if [[ $FAILURES -eq 0 ]]; then echo "Phase 10 smoke: all checks passed"; else echo "Phase 10 smoke: $FAILURES check(s) failed"; exit 1; fi
