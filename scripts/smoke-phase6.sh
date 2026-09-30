#!/usr/bin/env bash
# Phase 6 end-to-end smoke test on a throwaway Compose stack: a project with its own Compose
# services (PostgreSQL and a web app), verification in an isolated test environment with the
# Test Runner and the Browser Runner, test evidence, the Quality Gate (the only path to
# READY_FOR_MERGE), an approved merge with post-merge verification, and a failing test
# blocking READY_FOR_MERGE.
#
# The cross-review needs a real provider login; this script records one directly in the
# database as a stand-in (a real review is part of the operator review). Nothing else is faked.
#
# Usage: scripts/smoke-phase6.sh   (needs Docker, Internet for image pulls, ./secrets, and `make images`)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ho-smoke6
IDENTITY=smoke6
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ho-smoke6.XXXXXX")"
ROOT="$WORK/HermesProjects"
REPO="$ROOT/store"
ENV_FILE="$WORK/smoke.env"
FAILURES=0
POSTGRES="postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
PYTHON="python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e"
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
  for _ in $(seq 1 90); do
    value="$("$@" 2>/dev/null || true)"
    [[ "$value" == "$expected" ]] && break
    sleep 3
  done
  echo "$value"
}
task_state() { ho task show "$1" | field 'd["state"]'; }
verification_state() { ho tests show "$1" | field 'd["verifications"][0]["state"] if d["verifications"] else "NONE"'; }
cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    docker ps -aq --filter label=ho.kind=test-service --filter "label=ho.project=store" | xargs -r docker rm -f >/dev/null 2>&1 || true
    docker volume rm "cred-codex-$IDENTITY" >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

echo "== setup: a project with its own Compose services"
mkdir -p "$REPO/.hermes" "$REPO/tests" "$WORK/project-secrets"
cat > "$REPO/.hermes/project.yaml" <<'YAML'
version: 1
project: {name: store}
toolchain: {profiles: [python]}
agents: {allowed_providers: [claude, codex]}
commands:
  build: "python -m compileall -q store tests"
  test: "python -m unittest -v"
quality_gate: {lint: false, browser_tests: true}
browser_tests: {enabled: true, base_url: "http://app:8000"}
test_environment: {compose_files: [compose.yaml], services: [db, app], startup_timeout_seconds: 180}
YAML
cat > "$REPO/compose.yaml" <<YAML
services:
  db:
    image: $POSTGRES
    environment: {POSTGRES_PASSWORD: dev}
    ports: ["5432:5432"]
    healthcheck: {test: ["CMD", "pg_isready", "-U", "postgres"], interval: 1s, retries: 60}
  app:
    image: $PYTHON
    command: ["sh", "-c", "mkdir -p /s && echo '<h1>Store</h1>' > /s/index.html && cd /s && python -m http.server 8000"]
    ports: ["8000:8000"]
    healthcheck: {test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000')"], interval: 1s, retries: 60}
YAML
mkdir -p "$REPO/store"
printf 'def total(items):\n    return sum(items)\n' > "$REPO/store/__init__.py"
cat > "$REPO/tests/test_store.py" <<'PY'
import socket
import unittest
import urllib.request

from store import total


class StoreTest(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total([1, 2]), 3)

    def test_services_are_reachable(self):
        socket.create_connection(("db", 5432), timeout=5).close()
        self.assertIn(b"Store", urllib.request.urlopen("http://app:8000", timeout=5).read())
PY
touch "$REPO/tests/__init__.py"
git -C "$REPO" init -q -b main && git -C "$REPO" add -A && git -C "$REPO" commit -qm "initial store"
printf 'HO_MACHINE_PROFILE=mac-m2-pro\nHO_PROJECTS_ROOT_HOST=%s\nHO_VERSION=dev\nHO_PROVIDER_IDENTITY=%s\nHO_PROJECT_SECRETS_HOST=%s\n' \
  "$ROOT" "$IDENTITY" "$WORK/project-secrets" > "$ENV_FILE"
docker volume create --label "ho.credential=codex/$IDENTITY" "cred-codex-$IDENTITY" >/dev/null
dc up -d --build --wait >/dev/null
ho project register "$REPO" >/dev/null
ho approval approve "$(ho project scan store | field 'd["approval"]["id"]')" >/dev/null

new_change() {  # new_change <title> <test body>
  local task ws
  task="$(ho task create store "$1" | field 'd["key"]')"
  wait_for READY task_state "$task" >/dev/null
  ws="$(ho git workspace "$task" | field 'd["path"]')"
  local exec_id
  exec_id="$(ho execution run "$task" "printf '\\ndef discount(amount):\\n    return amount * 0.9\\n' >> store/__init__.py
    printf '\\n\\nclass DiscountTest(unittest.TestCase):\\n    def test_discount(self):\\n        from store import discount\\n        $2\\n' >> tests/test_store.py
    git add -A && git commit -qm 'Add discount'" --role DEVELOPER --provider codex --workspace "$ws" --workspace-access WRITE | field 'd["id"]')"
  wait_for SUCCEEDED sh -c "docker compose -p $PROJECT --env-file $ENV_FILE exec -T control-plane ho execution show $exec_id | python3 -c \"import json,sys; print(json.load(sys.stdin)['state'])\"" >/dev/null
  echo "$task"
}

echo "== verification in an isolated test environment"
TASK="$(new_change "Add a discount" "self.assertEqual(discount(10), 9.0)")"
ho git integrate "$TASK" >/dev/null
check "verification passed" "PASSED" "$(wait_for PASSED verification_state "$TASK")"
RESULTS="$(ho tests show "$TASK")"
check "steps: build, test (full suite), browser" "browser:PASSED build:PASSED test:PASSED" \
  "$(field '" ".join(sorted(f"{r["kind"]}:{r["status"]}" for r in d["verifications"][0]["runs"]))' <<<"$RESULTS")"
check "test services were started from the project's Compose" "True" \
  "$(field 'd["verifications"][0]["environment"]["project"].startswith("ho-")' <<<"$RESULTS")"
check "test services removed afterwards" "0" "$(wait_for 0 sh -c "docker ps -aq --filter label=ho.kind=test-service --filter label=ho.project=store | wc -l | tr -d ' '")"
SCREENSHOT="$(dc exec -T control-plane sh -c "ls /var/lib/ho/artifacts/*/*/executions/*/ | grep -c 'browser__page0.png'" || true)"
check "browser screenshot stored as evidence" "1" "$SCREENSHOT"
check "tests read the project's database and app (FULL_SUITE log)" "True" \
  "$([[ "$(dc exec -T control-plane sh -c "cat /var/lib/ho/artifacts/*/*/executions/*/*steps__test.log")" == *"test_services_are_reachable"*"... ok"* ]] && echo True || echo False)"

echo "== Quality Gate: the only path to READY_FOR_MERGE"
COMMIT="$(ho git status "$TASK" | field 'd["changes"]["integration_sha"]')"
sql "UPDATE tasks SET state = 'QUALITY_GATE', resume_state = NULL WHERE key = '$TASK'" >/dev/null  # Phase 7 orchestrator stand-in
check "gate fails without a cross-review" "FAIL" "$(ho gate evaluate "$TASK" | field 'd["outcome"]')"
check "task sent back to FIX_REQUIRED" "FIX_REQUIRED" "$(task_state "$TASK")"
# Stand-in for a REVIEWER execution by a second provider (needs a real login; see the operator review).
sql "INSERT INTO reviews (id, task_id, execution_id, commit_sha, reviewer_provider, developer_providers, outcome, requirements_met, summary)
     SELECT gen_random_uuid(), t.id, e.id, '$COMMIT', 'claude', '{}', 'APPROVED', true, 'smoke stand-in review'
     FROM tasks t JOIN executions e ON e.task_id = t.id WHERE t.key = '$TASK' ORDER BY e.created_at LIMIT 1" >/dev/null
sql "UPDATE tasks SET state = 'QUALITY_GATE', resume_state = NULL WHERE key = '$TASK'" >/dev/null
EVAL="$(ho gate evaluate "$TASK")"
check "gate passes with evidence and review" "PASS" "$(field 'd["outcome"]' <<<"$EVAL")"
check "READY_FOR_MERGE" "READY_FOR_MERGE" "$(task_state "$TASK")"

echo "== approved merge, post-merge verification"
MERGE="$(ho git merge-request "$TASK" | field 'd["id"]')"
check "merge approval binds the gate evaluation" "True" "$(ho approval show "$MERGE" | field 'bool(d["subject"]["quality_gate"])')"
ho approval approve "$MERGE" >/dev/null
check "DONE after post-merge verification" "DONE" "$(wait_for DONE task_state "$TASK")"
check "merged into main" "Merge $TASK (merge, approval $MERGE)" "$(git -C "$REPO" log -1 --format=%s)"
check "post-merge verification recorded" "POST_MERGE:PASSED" \
  "$(ho tests show "$TASK" | field '"{0}:{1}".format(d["verifications"][0]["purpose"], d["verifications"][0]["state"])')"

echo "== a failing test blocks READY_FOR_MERGE"
BAD="$(new_change "Add a wrong discount" "self.assertEqual(discount(10), 8.0)")"
ho git integrate "$BAD" >/dev/null
check "verification failed" "FAILED" "$(wait_for FAILED verification_state "$BAD")"
sql "UPDATE tasks SET state = 'QUALITY_GATE', resume_state = NULL WHERE key = '$BAD'" >/dev/null
BADEVAL="$(ho gate evaluate "$BAD")"
check "gate fails on tests" "FAIL:FAIL" "$(field '"{0}:{1}".format(d["outcome"], next(r["status"] for r in d["requirements"] if r["name"] == "tests"))' <<<"$BADEVAL")"
check "task not READY_FOR_MERGE" "FIX_REQUIRED" "$(task_state "$BAD")"
check "merge request refused" "1" "$(ho git merge-request "$BAD" >/dev/null 2>&1; echo $?)"

if [[ "$FAILURES" -ne 0 ]]; then
  echo "$FAILURES check(s) failed"
  exit 1
fi
echo "all checks passed"
