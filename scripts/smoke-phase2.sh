#!/usr/bin/env bash
# Phase 2 end-to-end smoke test against a real Compose stack.
#
# Creates a throwaway projects root with a sample repository, starts the stack
# under a separate Compose project name, exercises registration, onboarding,
# approval, task creation, scheduling, restart persistence, and Redis loss,
# then removes everything (unless KEEP=1).
#
# Usage: scripts/smoke-phase2.sh            (needs Docker and ./secrets from init-secrets.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ho-smoke
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ho-smoke.XXXXXX")"
ROOT="$WORK/HermesProjects"
ENV_FILE="$WORK/smoke.env"
FAILURES=0

dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
ho() { dc exec -T control-plane ho "$@"; }
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }
check() {  # check <description> <expected> <actual>
  if [[ "$2" == "$3" ]]; then echo "  ok    $1"; else echo "  FAIL  $1: expected '$2', got '$3'"; FAILURES=$((FAILURES + 1)); fi
}
cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$WORK"; fi
}
trap cleanup EXIT

echo "== sample repository"
mkdir -p "$ROOT/sample-app/src/auth" "$ROOT/sample-app/tests"
cat > "$ROOT/sample-app/package.json" <<'JSON'
{"name": "sample-app", "scripts": {"build": "tsc", "test": "vitest", "lint": "eslint ."},
 "devDependencies": {"typescript": "5", "vitest": "2"}}
JSON
echo '{}' > "$ROOT/sample-app/package-lock.json"
echo 'export {}' > "$ROOT/sample-app/src/auth/login.ts"
echo 'test' > "$ROOT/sample-app/tests/login.test.ts"
git -C "$ROOT/sample-app" init -q -b main
git -C "$ROOT/sample-app" add -A
git -C "$ROOT/sample-app" -c user.name=smoke -c user.email=smoke@example.com commit -qm init
printf 'HO_MACHINE_PROFILE=mac-m2-pro\nHO_PROJECTS_ROOT_HOST=%s\nHO_VERSION=dev\n' "$ROOT" > "$ENV_FILE"
echo "HO_HERMES_DASHBOARD_PORT=19202" >> "$ENV_FILE"  # never collide with the operator stack's Hermes

echo "== start stack"
dc up -d --build --wait >/dev/null
check "readiness" "ok" "$(ho health | field 'd["status"]')"

echo "== registration"
check "register inside root" "REGISTERED" "$(ho project register "$ROOT/sample-app" | field 'd["status"]')"
check "reject path outside root" "bad_request" "$(ho project register "$HOME" | field 'd["error"]' || true)"

echo "== onboarding"
SCAN="$(ho project scan sample-app)"
check "scan proposes configuration" "PROPOSED" "$(echo "$SCAN" | field 'd["project"]["status"]')"
check "node toolchain detected" "['node']" "$(echo "$SCAN" | field 'd["config"]["effective_config"]["toolchain"]["profiles"]')"
check "hard policy protects main" "True" "$(echo "$SCAN" | field '"main" in d["config"]["effective_config"]["git"]["protected_branches"]')"
APPROVAL="$(echo "$SCAN" | field 'd["approval"]["id"]')"

echo "== task before and after approval"
TASK="$(ho task create sample-app "Add OAuth authentication" --priority HIGH | field 'd["key"]')"
check "task waits in BACKLOG" "BACKLOG" "$(ho task show "$TASK" | field 'd["state"]')"
check "approval consumed" "CONSUMED" "$(ho approval approve "$APPROVAL" --note smoke | field 'd["state"]')"
sleep 3
check "scheduler promotes to READY" "READY" "$(ho task show "$TASK" | field 'd["state"]')"
check "no workers in Phase 2" "NoWorkersDispatcher" "$(ho task queue | field 'd["dispatcher"]')"

echo "== persistence across restart"
dc restart control-plane >/dev/null
dc up -d --wait control-plane >/dev/null
check "task survives restart" "READY" "$(ho task show "$TASK" | field 'd["state"]')"
check "project survives restart" "PROJECT_READY" "$(ho project show sample-app | field 'd["status"]')"

echo "== full stack restart (reboot-like)"
dc down >/dev/null
dc up -d --wait >/dev/null
check "task survives stack restart" "READY" "$(ho task show "$TASK" | field 'd["state"]')"

echo "== Redis loss is not data loss"
dc stop redis >/dev/null
check "readiness degraded, not failed" "degraded" "$(ho health | field 'd["status"]')"
check "pause works without Redis" "PAUSED" "$(ho task pause "$TASK" | field 'd["state"]')"
dc start redis >/dev/null
dc up -d --wait redis >/dev/null
check "resume after Redis returns" "READY" "$(ho task resume "$TASK" | field 'd["state"]')"

echo "== policy"
check "force push is high risk" "HIGH_RISK" "$(ho policy check 'git push --force origin main' | field 'd["class"]')"

echo "== isolation"
check "control plane has no Internet route" "blocked" \
  "$(dc exec -T control-plane python -c "import urllib.request
try:
    urllib.request.urlopen('https://example.com', timeout=5); print('reachable')
except Exception:
    print('blocked')")"
# Since Phase 5 git-service is the only writer of project repositories; nothing else mounts them writable.
check "projects root read-only outside git-service" "absent|read-only" \
  "$(dc exec -T control-plane sh -c 'test -e /projects && echo present || echo absent')|$(dc exec -T agent-manager sh -c 'touch /projects/x 2>/dev/null && echo writable || echo read-only')"
check "services run as non-root" "10001" "$(dc exec -T control-plane id -u)"

echo "== audit trail"
check "state history recorded" "BACKLOG>READY>PAUSED>READY" \
  "$(ho task events "$TASK" | field '">".join(["BACKLOG"] + [e["data"]["to"] for e in d["events"] if e["type"] == "TASK_STATE_CHANGED"])')"

if [[ "$FAILURES" -ne 0 ]]; then
  echo "$FAILURES check(s) failed"
  exit 1
fi
echo "all checks passed"
