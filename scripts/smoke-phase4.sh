#!/usr/bin/env bash
# Phase 4 end-to-end smoke test on a throwaway Compose stack: agent executions
# through the adapters, the real Claude Code and Codex CLIs reaching their
# providers only through the egress proxy, AUTH_REQUIRED and resume after
# login, secrets delivery and redaction, and task environment cleanup.
#
# Uses a throwaway provider identity ("smoke4") with deliberately invalid
# logins, so it never touches the operator's real credential volumes and makes
# no billable model request.
#
# Usage: scripts/smoke-phase4.sh   (needs Docker, Internet, ./secrets, and `make images`)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ho-smoke4
IDENTITY=smoke4
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ho-smoke4.XXXXXX")"
ROOT="$WORK/HermesProjects"
SECRETS="$WORK/project-secrets"
ENV_FILE="$WORK/smoke.env"
SECRET_VALUE="smoke-secret-7f3a9c2e41"
FAILURES=0

dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
ho() { dc exec -T control-plane ho "$@"; }
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }
check() {
  if [[ "$2" == "$3" ]]; then echo "  ok    $1"; else echo "  FAIL  $1: expected '$2', got '$3'"; FAILURES=$((FAILURES + 1)); fi
}
wait_exec() {  # wait_exec <execution> ; prints the final state
  local state=""
  for _ in $(seq 1 120); do
    state="$(ho execution show "$1" | field 'd["state"]')"
    [[ "$state" =~ ^(SUCCEEDED|FAILED|CANCELLED|LOST)$ ]] && break
    sleep 2
  done
  echo "$state"
}
wait_task() {  # wait_task <task> <state>
  local state=""
  for _ in $(seq 1 60); do
    state="$(ho task show "$1" | field 'd["state"]')"
    [[ "$state" == "$2" ]] && break
    sleep 2
  done
  echo "$state"
}
artifact() {  # artifact <execution> <name suffix> ; prints the stored artifact
  dc exec -T control-plane sh -c "cat /var/lib/ho/artifacts/*/*/executions/$1/*$2 2>/dev/null"
}
fake_login() {  # fake_login <provider> ; an invalid login in the smoke identity's credential volume
  local volume="cred-$1-$IDENTITY" file content
  docker volume rm "$volume" >/dev/null 2>&1 || true
  docker volume create --label "ho.credential=$1/$IDENTITY" "$volume" >/dev/null
  if [[ $1 == claude ]]; then
    file=oauth_token content='sk-ant-oat01-smoke-invalid'
  else
    file=auth.json content='{"tokens":{"id_token":"eyJhbGciOiJub25lIn0.eyJlbWFpbCI6ImFAYi5jIn0.x","access_token":"invalid","refresh_token":"invalid","account_id":"a"},"last_refresh":"2026-09-01T00:00:00Z"}'
  fi
  docker run --rm --network none --user 0 -v "$volume:/c" --entrypoint /bin/sh \
    "hermes-orchestrator/agent-base:${HO_VERSION:-dev}" \
    -c "printf '%s' '$content' > /c/$file && chown -R 10001:10001 /c && chmod 700 /c && chmod 600 /c/$file"
}
cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    docker volume rm "cred-claude-$IDENTITY" "cred-codex-$IDENTITY" >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

echo "== setup"
mkdir -p "$ROOT/sample-app/.hermes" "$SECRETS/sample-app/test"
cat > "$ROOT/sample-app/.hermes/project.yaml" <<'YAML'
version: 1
project: {name: sample-app}
toolchain: {profiles: [generic]}
agents: {allowed_providers: [claude, codex]}
secrets:
  - {name: TEST_TOKEN, environment: test}
YAML
echo '{"name": "sample-app", "scripts": {"test": "true"}}' > "$ROOT/sample-app/package.json"
git -C "$ROOT/sample-app" init -q -b main
git -C "$ROOT/sample-app" add package.json .hermes/project.yaml
git -C "$ROOT/sample-app" -c user.name=smoke -c user.email=smoke@example.com commit -qm init
printf '%s\n' "$SECRET_VALUE" > "$SECRETS/sample-app/test/TEST_TOKEN"
chmod 600 "$SECRETS/sample-app/test/TEST_TOKEN"
printf 'HO_MACHINE_PROFILE=mac-m2-pro\nHO_PROJECTS_ROOT_HOST=%s\nHO_VERSION=dev\nHO_PROVIDER_IDENTITY=%s\nHO_PROJECT_SECRETS_HOST=%s\n' \
  "$ROOT" "$IDENTITY" "$SECRETS" > "$ENV_FILE"
echo "HO_HERMES_DASHBOARD_PORT=19204" >> "$ENV_FILE"  # never collide with the operator stack's Hermes
docker volume rm "cred-claude-$IDENTITY" "cred-codex-$IDENTITY" >/dev/null 2>&1 || true
dc up -d --build --wait >/dev/null
check "agent-manager ready" "True" "$(ho health | field 'd["checks"].get("agent_manager")')"

ho project register "$ROOT/sample-app" >/dev/null
APPROVAL="$(ho project scan sample-app | field 'd["approval"]["id"]')"
ho approval approve "$APPROVAL" >/dev/null
new_task() { ho task create sample-app "$1" | field 'd["key"]'; }
T1="$(new_task "Phase 4 smoke: Codex")"
T2="$(new_task "Phase 4 smoke: Claude")"
T3="$(new_task "Phase 4 smoke: secrets")"
check "task READY" "READY" "$(wait_task "$T3" READY)"
check "provider status lists both adapters" "claude,codex" "$(ho auth status | field '",".join(sorted(p["provider"] for p in d["providers"]))')"

echo "== no login: the execution fails as AUTH and the task waits"
W1="$(ho git workspace "$T1" | field 'd["path"]')"
E1="$(ho agent run "$T1" "Add a README" --provider codex --workspace "$W1" --workspace-access WRITE | field 'd["id"]')"
check "execution failed" "FAILED" "$(wait_exec "$E1")"
check "failure class AUTH" "AUTH" "$(ho execution show "$E1" | field 'd["failure_class"]')"
check "task AUTH_REQUIRED" "AUTH_REQUIRED" "$(wait_task "$T1" AUTH_REQUIRED)"
check "AUTH_REQUIRED event" "True" "$(ho task events "$T1" | field '"AUTH_REQUIRED" in {e["type"] for e in d["events"]}')"

echo "== login confirmed: the task resumes and the assignment runs again (real Codex CLI)"
fake_login codex
READY_OUT="$(ho auth ready codex --identity "$IDENTITY")"
check "task resumed" "['$T1']" "$(field 'd["tasks_resumed"]' <<<"$READY_OUT")"
E1B="$(field 'd["executions_continued"][0]["continued_by"]' <<<"$READY_OUT")"
check "continued execution ran and failed as AUTH (invalid login)" "FAILED/AUTH" \
  "$(wait_exec "$E1B")/$(ho execution show "$E1B" | field 'd["failure_class"]')"
check "task waits again" "AUTH_REQUIRED" "$(wait_task "$T1" AUTH_REQUIRED)"
check "credential marked AUTH_REQUIRED" "AUTH_REQUIRED" \
  "$(ho auth status | field 'next(i["status"] for i in d["identities"] if i["provider"] == "codex" and i["identity"] == "'$IDENTITY'")')"
EGRESS="$(artifact "$E1B" egress.jsonl)"
check "Codex reached OpenAI through the proxy" "True" \
  "$([[ "$EGRESS" == *'"host": "chatgpt.com"'* || "$EGRESS" == *'"host": "auth.openai.com"'* ]] && echo True || echo False)"
check "no denied egress" "False" "$([[ "$EGRESS" == *EGRESS_DENIED* ]] && echo True || echo False)"
RESULT="$(artifact "$E1B" result.json)"
check "normalized result stored" "AUTH" "$(field 'd["failure_class"]' <<<"$RESULT")"
check "raw provider stream not stored" "" "$(dc exec -T control-plane sh -c "ls /var/lib/ho/artifacts/*/*/executions/$E1B/ | grep ho__ || true")"

echo "== Claude Code with an invalid login (real CLI)"
fake_login claude
W2="$(ho git workspace "$T2" | field 'd["path"]')"
E2="$(ho agent run "$T2" "Review the project" --provider claude --role REVIEWER --workspace "$W2" --workspace-access READ | field 'd["id"]')"
check "claude execution failed as AUTH" "FAILED/AUTH" "$(wait_exec "$E2")/$(ho execution show "$E2" | field 'd["failure_class"]')"
check "claude session captured for resume" "True" "$(ho execution show "$E2" | field 'bool(d["provider_session_id"])')"
check "Claude reached Anthropic through the proxy" "True" \
  "$([[ "$(artifact "$E2" egress.jsonl)" == *'"host": "api.anthropic.com"'* ]] && echo True || echo False)"
check "token not in logs" "False" "$([[ "$(artifact "$E2" logs.txt)" == *sk-ant-oat01-smoke* ]] && echo True || echo False)"
check "claude task AUTH_REQUIRED" "AUTH_REQUIRED" "$(wait_task "$T2" AUTH_REQUIRED)"

echo "== secrets: delivered by reference, redacted on the way out"
E3="$(ho execution run "$T3" 'cat /run/ho/secrets/TEST_TOKEN; echo; stat -c "mode=%a" /run/ho/secrets/TEST_TOKEN' --secret TEST_TOKEN | field 'd["id"]')"
check "secret execution succeeded" "SUCCEEDED" "$(wait_exec "$E3")"
check "grant holds a reference" "['sample-app/test/TEST_TOKEN']" "$(ho execution show "$E3" | field 'd["grant"]["capabilities"]["secrets"]')"
LOGS="$(artifact "$E3" logs.txt)"
check "value redacted from logs" "True" "$([[ "$LOGS" == *'[REDACTED:TEST_TOKEN]'* && "$LOGS" != *$SECRET_VALUE* ]] && echo True || echo False)"
check "secret file mode 0600" "True" "$([[ "$LOGS" == *mode=600* ]] && echo True || echo False)"
check "value not in PostgreSQL" "0" "$(dc exec -T postgres psql -U ho_owner -d ho -tAc "SELECT count(*) FROM executions WHERE spec::text LIKE '%$SECRET_VALUE%'")"

echo "== task end releases the session store"
check "session volume exists while the task is open" "1" "$(docker volume ls -q --filter "name=ho-sess-$PROJECT-$(tr 'A-Z' 'a-z' <<<"$T2")-claude" | wc -l | tr -d ' ')"
ho task cancel "$T2" >/dev/null
for _ in $(seq 1 20); do
  [[ -z "$(docker volume ls -q --filter "name=ho-sess-$PROJECT-$(tr 'A-Z' 'a-z' <<<"$T2")-claude")" ]] && break
  sleep 2
done
check "session volume removed after cancel" "0" "$(docker volume ls -q --filter "name=ho-sess-$PROJECT-$(tr 'A-Z' 'a-z' <<<"$T2")-claude" | wc -l | tr -d ' ')"
check "credential volumes untouched by cleanup" "2" "$(docker volume ls -q --filter "label=ho.credential" | grep -c "$IDENTITY" || true)"

if [[ "$FAILURES" -ne 0 ]]; then
  echo "$FAILURES check(s) failed"
  exit 1
fi
echo "all checks passed"
