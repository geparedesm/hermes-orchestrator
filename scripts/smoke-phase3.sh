#!/usr/bin/env bash
# Phase 3 end-to-end smoke test: the control plane requests real workers and the
# Compose agent-manager creates them in Docker.
#
# Usage: scripts/smoke-phase3.sh     (needs Docker, Internet, ./secrets, and `make images`)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ho-smoke3
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ho-smoke3.XXXXXX")"
ROOT="$WORK/HermesProjects"
ENV_FILE="$WORK/smoke.env"
# A throwaway provider identity, so the operator's real login (cred-codex-default) is never touched.
CREDENTIAL=cred-codex-smoke3
FAILURES=0

dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
ho() { dc exec -T control-plane ho "$@"; }
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }
check() {
  if [[ "$2" == "$3" ]]; then echo "  ok    $1"; else echo "  FAIL  $1: expected '$2', got '$3'"; FAILURES=$((FAILURES + 1)); fi
}
wait_state() {  # wait_state <execution> <expected> ; prints the final state
  local state=""
  for _ in $(seq 1 60); do
    state="$(ho execution show "$1" | field 'd["state"]')"
    [[ "$state" == "$2" || "$state" =~ ^(SUCCEEDED|FAILED|CANCELLED|LOST)$ ]] && break
    sleep 2
  done
  echo "$state"
}
cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    docker volume rm "$CREDENTIAL" >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

echo "== setup"
mkdir -p "$ROOT/sample-app"
echo '{"name": "sample-app", "scripts": {"test": "true"}}' > "$ROOT/sample-app/package.json"
git -C "$ROOT/sample-app" init -q -b main
git -C "$ROOT/sample-app" add package.json
git -C "$ROOT/sample-app" -c user.name=smoke -c user.email=smoke@example.com commit -qm init
printf 'HO_MACHINE_PROFILE=mac-m2-pro\nHO_PROJECTS_ROOT_HOST=%s\nHO_VERSION=dev\nHO_PROVIDER_IDENTITY=smoke3\nHO_PROJECT_SECRETS_HOST=%s\n' "$ROOT" "$WORK/project-secrets" > "$ENV_FILE"
echo "HO_HERMES_DASHBOARD_PORT=19203" >> "$ENV_FILE"  # never collide with the operator stack's Hermes
# Phase 4 creates provider credential volumes through a login flow; simulate an empty one.
mkdir -p "$WORK/project-secrets"
docker volume create --label ho.credential=codex/smoke3 "$CREDENTIAL" >/dev/null
dc up -d --build --wait >/dev/null
check "agent-manager ready" "True" "$(ho health | field 'd["checks"].get("agent_manager")')"

ho project register "$ROOT/sample-app" >/dev/null
APPROVAL="$(ho project scan sample-app | field 'd["approval"]["id"]')"
ho approval approve "$APPROVAL" >/dev/null
TASK="$(ho task create sample-app "Phase 3 smoke" | field 'd["key"]')"
sleep 6
check "task READY" "READY" "$(ho task show "$TASK" | field 'd["state"]')"

echo "== runner without network"
EXEC="$(ho execution run "$TASK" 'id -u; echo "{\"ok\": true}" > /output/result.json; curl -s --max-time 5 https://example.com >/dev/null && echo net=yes || echo net=no' | field 'd["id"]')"
check "runner succeeded" "SUCCEEDED" "$(wait_state "$EXEC" SUCCEEDED)"
check "outputs collected (result, logs)" "2" "$(ho execution show "$EXEC" | field 'len(d["artifacts"])')"
check "grant revoked" "True" "$(ho execution show "$EXEC" | field 'd["grant_revoked_at"] is not None')"
check "worker removed" "0" "$(docker ps -aq --filter "label=ho.execution=$EXEC" | wc -l | tr -d ' ')"

echo "== developer writes only its workspace"
WS="$(ho git workspace "$TASK" --suffix w1 | field 'd["path"]')"
EXEC="$(ho execution run "$TASK" 'echo from-worker > /workspace/hello.txt; touch /etc/x 2>/dev/null && echo rootfs=rw || echo rootfs=ro' \
  --role DEVELOPER --provider codex --workspace "$WS" --workspace-access WRITE | field 'd["id"]')"
check "developer succeeded" "SUCCEEDED" "$(wait_state "$EXEC" SUCCEEDED)"
check "file written to host worktree" "from-worker" "$(cat "$ROOT/sample-app/$WS/hello.txt" 2>/dev/null)"
check "workspace outside worktrees rejected" "bad_request" \
  "$(ho execution run "$TASK" true --workspace ../../etc --workspace-access READ | field 'd.get("error")' || true)"

echo "== egress through the proxy"
EXEC="$(ho execution run "$TASK" '
  curl -s -o /dev/null -w "public=%{http_code}\n" --max-time 20 https://example.com
  curl -s -o /dev/null --noproxy "*" --max-time 8 https://example.com && echo direct=yes || echo direct=no
  curl -s -o /dev/null -w "host=%{http_code}\n" --max-time 8 https://host.docker.internal
  true' \
  --role DEVELOPER --provider codex --egress STANDARD | field 'd["id"]')"
check "egress execution finished" "SUCCEEDED" "$(wait_state "$EXEC" SUCCEEDED)"
LOGS="$(dc exec -T control-plane sh -c "cat /var/lib/ho/artifacts/*/*/executions/$EXEC/*logs.txt")"
check "public HTTPS allowed through proxy" "True" "$([[ "$LOGS" == *"public=200"* ]] && echo True || echo False)"
check "direct connection blocked" "True" "$([[ "$LOGS" == *"direct=no"* ]] && echo True || echo False)"
check "host.docker.internal denied" "True" "$([[ "$LOGS" == *"host=000"* || "$LOGS" == *"host=403"* ]] && echo True || echo False)"

echo "== workers cannot reach platform services (N01)"
PG_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$(dc ps -q postgres)" | awk '{print $1}')"
CP_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$(dc ps -q control-plane)" | awk '{print $1}')"
EXEC="$(ho execution run "$TASK" "
  timeout 4 bash -c '</dev/tcp/$PG_IP/5432' 2>/dev/null && echo pg-ip=reached || echo pg-ip=blocked
  timeout 4 bash -c '</dev/tcp/$CP_IP/8080' 2>/dev/null && echo cp-ip=reached || echo cp-ip=blocked
  curl -s -o /dev/null -w 'pg-name=%{http_code}\n' --max-time 5 https://postgres:5432
  curl -s -o /dev/null -w 'cp-name=%{http_code}\n' --max-time 5 https://control-plane:8080
  true" --role DEVELOPER --provider codex --egress STANDARD | field 'd["id"]')"
check "probe finished" "SUCCEEDED" "$(wait_state "$EXEC" SUCCEEDED)"
PROBE="$(dc exec -T control-plane sh -c "cat /var/lib/ho/artifacts/*/*/executions/$EXEC/*logs.txt")"
check "postgres unreachable by IP" "True" "$([[ "$PROBE" == *"pg-ip=blocked"* ]] && echo True || echo False)"
check "control plane unreachable by IP" "True" "$([[ "$PROBE" == *"cp-ip=blocked"* ]] && echo True || echo False)"
check "platform names refused by the proxy" "True" "$([[ "$PROBE" == *"pg-name=000"* && "$PROBE" == *"cp-name=000"* ]] && echo True || echo False)"

echo "== agent-manager restart does not lose a running execution"
EXEC="$(ho execution run "$TASK" 'sleep 15; echo done' | field 'd["id"]')"
sleep 3
dc restart agent-manager >/dev/null
dc up -d --wait agent-manager >/dev/null
check "execution completes after restart" "SUCCEEDED" "$(wait_state "$EXEC" SUCCEEDED)"

echo "== cancelling the task stops its workers"
EXEC="$(ho execution run "$TASK" 'sleep 300' | field 'd["id"]')"
sleep 3
ho task cancel "$TASK" >/dev/null
check "execution cancelled" "CANCELLED" "$(wait_state "$EXEC" CANCELLED)"
sleep 3
check "no platform containers left for the task" "0" "$(docker ps -aq --filter "label=ho.task=$TASK" | wc -l | tr -d ' ')"

echo "== audit"
check "grant and worker events recorded" "True" \
  "$(ho task events "$TASK" | field 'all(t in {e["type"] for e in d["events"]} for t in ("GRANT_ISSUED", "WORKER_CREATED", "WORKER_STOPPED", "GRANT_REVOKED"))')"

if [[ "$FAILURES" -ne 0 ]]; then
  echo "$FAILURES check(s) failed"
  exit 1
fi
echo "all checks passed"
