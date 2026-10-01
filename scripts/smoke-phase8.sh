#!/usr/bin/env bash
# Phase 8 end-to-end smoke test on a throwaway Compose stack: recovery with real failures.
# The control plane is killed mid-execution and restarts without duplicating work; a worker that
# vanished while the control plane was down is reconciled as LOST; a full stack restart (reboot
# simulation) keeps tasks and checkpoints; Agent Manager down makes the platform DEGRADED and new
# executions wait instead of failing; Redis down does not stop scheduling; a cancelled task's
# unfinished commits are retained.
#
# Raw-command executions only: no provider login and no billable model request.
#
# Usage: scripts/smoke-phase8.sh   (needs Docker, ./secrets, and `make images`)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ho-smoke8
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ho-smoke8.XXXXXX")"
ROOT="$WORK/HermesProjects"
ENV_FILE="$WORK/smoke.env"
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
wait_for() {  # wait_for <expected> <command...>
  local expected=$1 value=""
  shift
  for _ in $(seq 1 60); do
    value="$("$@" 2>/dev/null || true)"
    [[ "$value" == "$expected" ]] && break
    sleep 2
  done
  echo "$value"
}
exec_state() { sql "SELECT state FROM executions WHERE id = '$1'"; }
worker() { docker ps -aq --filter "label=ho.execution=$1" --filter label=ho.kind=worker; }
health() { ho recovery status | field "d['health']['components'].get('$1', {}).get('state', 'HEALTHY')"; }
up() { dc up -d --wait >/dev/null 2>&1; }
cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

echo "== setup"
mkdir -p "$ROOT/app/.hermes"
cat > "$ROOT/app/.hermes/project.yaml" <<'YAML'
version: 1
project: {name: app}
toolchain: {profiles: [generic]}
agents: {allowed_providers: [claude, codex]}
YAML
echo "x = 1" > "$ROOT/app/app.py"
git -C "$ROOT/app" init -q -b main && git -C "$ROOT/app" add -A && git -C "$ROOT/app" commit -qm init
printf 'HO_MACHINE_PROFILE=mac-m2-pro\nHO_PROJECTS_ROOT_HOST=%s\nHO_VERSION=dev\nHO_PROVIDER_IDENTITY=smoke8\n' "$ROOT" > "$ENV_FILE"
dc up -d --build --wait >/dev/null
ho project register "$ROOT/app" >/dev/null
ho approval approve "$(ho project scan app | field 'd["approval"]["id"]')" >/dev/null
check "startup reconciliation recorded" "True" "$(ho recovery status | field 'any(r["trigger"] == "STARTUP" for r in d["recent_runs"])')"
TASK="$(ho task create app "Phase 8 smoke" | field 'd["key"]')"
wait_for READY sh -c "docker compose -p $PROJECT --env-file $ENV_FILE exec -T control-plane ho task show $TASK | python3 -c \"import json,sys; print(json.load(sys.stdin)['state'])\"" >/dev/null

echo "== control plane killed mid-execution"
LONG="$(ho execution run "$TASK" 'sleep 240' | field 'd["id"]')"
check "worker running" "RUNNING" "$(wait_for RUNNING exec_state "$LONG")"
docker kill "$(dc ps -q control-plane)" >/dev/null
up
check "execution survives the restart" "RUNNING" "$(wait_for RUNNING exec_state "$LONG")"
check "exactly one worker for it" "1" "$(worker "$LONG" | wc -l | tr -d ' ')"
check "startup reconciliation after the crash" "2" "$(sql "SELECT count(*) FROM recovery_runs WHERE trigger = 'STARTUP'")"

echo "== worker vanished while the control plane was down"
dc stop control-plane >/dev/null 2>&1
docker rm -f "$(worker "$LONG")" >/dev/null
up
check "vanished worker reconciled as LOST" "LOST" "$(wait_for LOST exec_state "$LONG")"

echo "== reboot simulation (whole stack restarted)"
CHECKPOINTS="$(sql "SELECT count(*) FROM task_checkpoints")"
dc restart >/dev/null 2>&1
up
check "task kept" "READY" "$(ho task show "$TASK" | field 'd["state"]')"
check "checkpoints kept" "$CHECKPOINTS" "$(sql "SELECT count(*) FROM task_checkpoints")"
AFTER="$(ho execution run "$TASK" 'echo ok' | field 'd["id"]')"
check "new work runs after the restart" "SUCCEEDED" "$(wait_for SUCCEEDED exec_state "$AFTER")"

echo "== Agent Manager down: DEGRADED, new work waits"
dc stop agent-manager >/dev/null 2>&1
check "platform DEGRADED" "DEGRADED" "$(wait_for DEGRADED health agent_manager)"
WAITING="$(ho execution run "$TASK" 'echo later' | field 'd["id"]')"
sleep 8
check "execution waits instead of failing" "REQUESTED" "$(exec_state "$WAITING")"
dc start agent-manager >/dev/null 2>&1
check "platform recovered" "HEALTHY" "$(wait_for HEALTHY health agent_manager)"
check "waiting execution ran" "SUCCEEDED" "$(wait_for SUCCEEDED exec_state "$WAITING")"
check "degradation and recovery recorded" "2" \
  "$(sql "SELECT count(*) FROM events WHERE type IN ('PLATFORM_DEGRADED', 'PLATFORM_RECOVERED')")"

echo "== Redis down: scheduling continues from PostgreSQL"
dc stop redis >/dev/null 2>&1
REDIS_TASK="$(ho task create app "Created while Redis is down" | field 'd["key"]')"
check "task scheduled without Redis" "READY" \
  "$(wait_for READY sh -c "docker compose -p $PROJECT --env-file $ENV_FILE exec -T control-plane ho task show $REDIS_TASK | python3 -c \"import json,sys; print(json.load(sys.stdin)['state'])\"")"
dc start redis >/dev/null 2>&1

echo "== cancelled work is retained"
WS="$(ho git workspace "$TASK" | field 'd["path"]')"
git -C "$ROOT/app/$WS" commit -qm "unfinished" --allow-empty
ho task cancel "$TASK" >/dev/null
ho recovery run >/dev/null
check "workspace retained with its commits" "RETAINED" "$(sql "SELECT status FROM workspaces WHERE task_id = (SELECT id FROM tasks WHERE key = '$TASK')")"
check "clone kept on disk" "True" "$([[ -d "$ROOT/app/$WS" ]] && echo True || echo False)"
check "no lease or queued launch left" "0" \
  "$(sql "SELECT (SELECT count(*) FROM task_leases) + (SELECT count(*) FROM pending_launches)")"

echo
if [[ $FAILURES -eq 0 ]]; then echo "Phase 8 smoke: all checks passed"; else echo "Phase 8 smoke: $FAILURES check(s) failed"; exit 1; fi
