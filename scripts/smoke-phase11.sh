#!/usr/bin/env bash
# Phase 11 operations smoke test on a throwaway Compose stack: PostgreSQL and Redis outages, backup and
# restore, an approval-controlled update, a failed update rolled back automatically, the health check,
# dependency caches, and daily maintenance. Raw-command executions only: no provider login or billable call.
#
# Usage: scripts/smoke-phase11.sh   (needs Docker, ./secrets, and `make images`)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ho-smoke11
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ho-smoke11.XXXXXX")"
ROOT="$WORK/HermesProjects"
ENV_FILE="$WORK/smoke.env"
FAILURES=0
export HO_COMPOSE_PROJECT="$PROJECT" HO_ENV_FILE="$ENV_FILE" HO_BACKUP_DIR="$WORK/backups"
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
task_state() { ho task show "$1" | field 'd["state"]'; }
exec_state() { sql "SELECT state FROM executions WHERE id = '$1'"; }
version() { grep -E '^HO_VERSION=' "$ENV_FILE" | cut -d= -f2; }
cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    docker volume ls -q --filter label=ho.kind=cache --filter label=ho.project=app | xargs -r docker volume rm >/dev/null 2>&1 || true
    docker images --format '{{.Repository}}:{{.Tag}}' | grep -E ':smoke11[ab]$' | xargs docker image rm >/dev/null 2>&1 || true
    docker volume rm cred-codex-smoke11 >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

echo "== setup"
mkdir -p "$ROOT/app/.hermes"
printf 'version: 1\nproject: {name: app}\ntoolchain: {profiles: [python]}\nagents: {allowed_providers: [claude, codex]}\ncommands: {test: "true"}\n' \
  > "$ROOT/app/.hermes/project.yaml"
echo "x = 1" > "$ROOT/app/app.py"
git -C "$ROOT/app" init -q -b main && git -C "$ROOT/app" add -A && git -C "$ROOT/app" commit -qm init
printf 'HO_MACHINE_PROFILE=mac-m2-pro\nHO_PROJECTS_ROOT_HOST=%s\nHO_VERSION=dev\nHO_PROVIDER_IDENTITY=smoke11\nHO_HERMES_DASHBOARD_PORT=19121\n' \
  "$ROOT" > "$ENV_FILE"
docker volume create --label "ho.credential=codex/smoke11" "cred-codex-smoke11" >/dev/null  # raw commands need no login
dc up -d --build --wait >/dev/null
ho project register "$ROOT/app" >/dev/null
ho approval approve "$(ho project scan app | field 'd["approval"]["id"]')" >/dev/null
T1="$(ho task create app "Before the backup" | field 'd["key"]')"
check "task ready" "READY" "$(wait_for READY task_state "$T1")"
check "health check passes" "0" "$(scripts/check.sh >/dev/null 2>&1; echo $?)"

echo "== PostgreSQL outage and reconnection"
dc stop postgres >/dev/null 2>&1
check "control plane reports the database down" "unavailable" \
  "$(wait_for unavailable sh -c "curl -s http://127.0.0.1:0 >/dev/null 2>&1; docker compose -p $PROJECT --env-file $ENV_FILE exec -T control-plane python -c \"import urllib.request,json; print(json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/health/ready')))['status'])\" 2>/dev/null || echo unavailable")"
dc start postgres >/dev/null 2>&1
dc up -d --wait postgres >/dev/null
T2="$(wait_for T-2 sh -c "docker compose -p $PROJECT --env-file $ENV_FILE exec -T control-plane ho task create app 'After the reconnection' | python3 -c \"import json,sys; print(json.load(sys.stdin)['key'])\"")"
check "control plane reconnected (new task accepted)" "T-2" "$T2"
check "scheduler resumed" "READY" "$(wait_for READY task_state "$T2")"

echo "== Redis restart"
dc restart redis >/dev/null 2>&1
T3="$(ho task create app "After the Redis restart" | field 'd["key"]')"
check "scheduling after a Redis restart" "READY" "$(wait_for READY task_state "$T3")"

echo "== dependency cache"
WS="$(ho git workspace "$T1" | field 'd["path"]')"
EXEC="$(ho execution run "$T1" 'mkdir -p "$PIP_CACHE_DIR/http" && echo cached > "$PIP_CACHE_DIR/http/entry"' \
  --role DEVELOPER --provider codex --workspace "$WS" --workspace-access WRITE | field 'd["id"]')"
check "execution with a cache" "SUCCEEDED" "$(wait_for SUCCEEDED exec_state "$EXEC")"
check "cache volume for the project's pip" "True" \
  "$(ho cache list | field 'any(c["volume"] == "ho-cache-app-pip" and c["ecosystem"] == "pip" for c in d["caches"])')"
check "cache kept for the next execution" "cached" \
  "$(docker run --rm -v ho-cache-app-pip:/c:ro --entrypoint cat "hermes-orchestrator/agent-base:dev" /c/http/entry 2>/dev/null)"
check "cache invalidation" "['ho-cache-app-pip']" "$(ho cache clear app --ecosystem pip | field 'd["removed"]')"

echo "== daily maintenance"
check "maintenance runs" "True" "$(ho maintenance run | field '"artifacts" in d and "history" in d')"

echo "== backup and restore"
scripts/backup.sh >"$WORK/backup.log" 2>&1
BACKUP="$(tail -1 "$WORK/backup.log" | sed 's/^backup complete: //')"
check "backup written with checksums" "True" "$([[ -s "$BACKUP/postgres.dump" && -s "$BACKUP/SHA256SUMS" && -s "$BACKUP/hermes-data.tar.gz" ]] && echo True || echo False)"
LISTING="$(tar -tzf "$BACKUP/hermes-data.tar.gz")"
CONFIG="$(tar -xzOf "$BACKUP/hermes-data.tar.gz" config.yaml 2>/dev/null || true)"
check "no credential files in the backup" "False" "$(grep -qE '(^|/)(\.env|auth\.json)$' <<<"$LISTING" && echo True || echo False)"
check "no secret values in Hermes's config backup" "False" \
  "$(grep -qE "secret:|password:|$(cat secrets/ho_hermes_dashboard_password)" <<<"$CONFIG" && echo True || echo False)"
check "no service secrets anywhere in the backup" "False" \
  "$(cd "$BACKUP" && for f in *.tar.gz; do tar -xzOf "$f"; done | strings | grep -qF "$(cat "$OLDPWD/secrets/ho_merge_key")" && echo True || echo False)"
T4="$(ho task create app "After the backup" | field 'd["key"]')"
scripts/restore.sh "$BACKUP" >"$WORK/restore.log" 2>&1 || { tail -5 "$WORK/restore.log"; }
check "restored state (later task gone)" "" "$(sql "SELECT key FROM tasks WHERE key = '$T4'")"
check "restored state (earlier task kept)" "$T1" "$(sql "SELECT key FROM tasks WHERE key = '$T1'")"
check "Hermes plugin enabled after restore" "0" "$(scripts/check.sh >/dev/null 2>&1; echo $?)"

echo "== approval-controlled update"
REQUEST="$(scripts/update.sh smoke11a)"
APPROVAL="$(sed -n 's/^update to smoke11a requested: approval //p' <<<"$REQUEST")"
check "update requested as an approval" "UPDATE" "$(ho approval show "$APPROVAL" | field 'd["action"]')"
check "update refused before approval" "1" "$(scripts/update.sh smoke11a "$APPROVAL" >/dev/null 2>&1; echo $?)"
ho approval approve "$APPROVAL" >/dev/null
check "an approval cannot deploy another version" "1" "$(scripts/update.sh smoke11z "$APPROVAL" >/dev/null 2>&1; echo $?)"
check "approval not consumed by the refused attempt" "APPROVED" "$(ho approval show "$APPROVAL" | field 'd["state"]')"
scripts/update.sh smoke11a "$APPROVAL" >"$WORK/update.log" 2>&1 || tail -5 "$WORK/update.log"
check "updated version" "smoke11a" "$(version)"
check "update recorded" "SUCCEEDED" "$(ho update list | field 'd["updates"][0]["state"]')"
check "running the new version" "smoke11a" "$(ho update list | field 'd["version"]')"

echo "== failed update rolled back"
APPROVAL="$(scripts/update.sh smoke11b | sed -n 's/^update to smoke11b requested: approval //p')"
ho approval approve "$APPROVAL" >/dev/null
check "failing update exits non-zero" "1" "$(HO_UPDATE_INJECT_FAILURE=1 scripts/update.sh smoke11b "$APPROVAL" >"$WORK/failed.log" 2>&1; echo $?)"
check "previous version restored" "smoke11a" "$(version)"
check "rollback recorded" "ROLLED_BACK" "$(ho update list | field 'd["updates"][0]["state"]')"
check "healthy after rollback" "0" "$(scripts/check.sh >/dev/null 2>&1; echo $?)"

echo
if [[ $FAILURES -eq 0 ]]; then echo "Phase 11 smoke: all checks passed"; else echo "Phase 11 smoke: $FAILURES check(s) failed"; exit 1; fi
