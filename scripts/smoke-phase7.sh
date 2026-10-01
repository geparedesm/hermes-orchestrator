#!/usr/bin/env bash
# Phase 7 end-to-end smoke test on a throwaway Compose stack with HO_ORCHESTRATION=true:
# a READY task is led by the orchestrator (lease, PLANNING, a real ORCHESTRATOR container with
# read access to the project), a provider login failure moves the lead to the other provider at
# a step boundary (new epoch), the task waits for a login only when no provider is left, budgets
# (visible, raised through an approval), live requirement revision, duplicate detection, and an
# on-demand Task Manifest validated against the schema.
#
# Uses a throwaway provider identity ("smoke7") with deliberately invalid logins, so it never
# touches the operator's real credentials and makes no billable model request. The full
# develop -> cross-review -> integrate -> verify -> gate cycle with real providers is the
# operator run documented in docs/validation/phase-7.md.
#
# Usage: scripts/smoke-phase7.sh   (needs Docker, Internet, ./secrets, and `make images`)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ho-smoke7
IDENTITY=smoke7
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ho-smoke7.XXXXXX")"
ROOT="$WORK/HermesProjects"
ENV_FILE="$WORK/smoke.env"
FAILURES=0

dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
ho() { dc exec -T control-plane ho "$@"; }
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
    sleep 2
  done
  echo "$value"
}
task_state() { ho task show "$1" | field 'd["state"]'; }
events() { ho task events "$1" | field '" ".join(e["type"] for e in d["events"])'; }
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

echo "== setup (HO_ORCHESTRATION=true)"
mkdir -p "$ROOT/notes/.hermes"
cat > "$ROOT/notes/.hermes/project.yaml" <<'YAML'
version: 1
project: {name: notes}
toolchain: {profiles: [python]}
agents: {allowed_providers: [claude, codex]}
commands: {test: "python -m unittest"}
YAML
printf 'def add(a, b):\n    return a + b\n' > "$ROOT/notes/notes.py"
git -C "$ROOT/notes" init -q -b main
git -C "$ROOT/notes" add -A
git -C "$ROOT/notes" -c user.name=smoke -c user.email=smoke@example.com commit -qm init
printf 'HO_MACHINE_PROFILE=mac-m2-pro\nHO_PROJECTS_ROOT_HOST=%s\nHO_VERSION=dev\nHO_PROVIDER_IDENTITY=%s\nHO_ORCHESTRATION=true\n' \
  "$ROOT" "$IDENTITY" > "$ENV_FILE"
echo "HO_HERMES_DASHBOARD_PORT=19207" >> "$ENV_FILE"  # never collide with the operator stack's Hermes
fake_login claude
fake_login codex
dc up -d --build --wait >/dev/null
ho project register "$ROOT/notes" >/dev/null
ho approval approve "$(ho project scan notes | field 'd["approval"]["id"]')" >/dev/null

echo "== the orchestrator leads a READY task"
TASK="$(ho task create notes "Add a subtract function with tests" | field 'd["key"]')"
check "task is planned by the orchestrator" "PLANNING" "$(wait_for PLANNING task_state "$TASK")"
INSPECT="$(ho task inspect "$TASK")"
check "lease taken by claude (epoch 1)" "claude 1" "$(field 'f"{d["lease"]["provider"]} {d["lease"]["epoch"]}"' <<<"$INSPECT")"
STEP="$(ho execution list --task "$TASK" | field 'next(e["id"] for e in d["executions"] if e["role"] == "ORCHESTRATOR")')"
SHOW="$(ho execution show "$STEP")"
check "orchestrator step reads the project, no workspace" "True None" \
  "$(field 'f"{bool(d["grant"]["capabilities"]["project_read"])} {d.get("workspace")}"' <<<"$SHOW")"
check "budget reserved for the step" "True" "$(ho task budget "$TASK" | field 'd["reserved"].get("provider_usage_units", 0) > 0')"

echo "== login failures: failover at a step boundary, then wait for a login"
check "task waits for a login once both providers failed" "AUTH_REQUIRED" "$(wait_for AUTH_REQUIRED task_state "$TASK")"
check "orchestration failed over to codex (epoch 2)" "codex 2" \
  "$(ho task inspect "$TASK" | field 'f"{d["lease"]["provider"]} {d["lease"]["epoch"]}"')"
check "FAILOVER_COMPLETED recorded" "True" "$([[ "$(events "$TASK")" == *FAILOVER_COMPLETED* ]] && echo True || echo False)"
check "reservations settled" "0" "$(ho task budget "$TASK" | field 'd["reserved"].get("provider_usage_units", 0)')"

echo "== budgets and revisions"
APPROVAL="$(ho task budget "$TASK" --add agent_launches=5 | field 'd["id"]')"
BEFORE="$(ho task budget "$TASK" | field 'd["limits"]["agent_launches"]')"
ho approval approve "$APPROVAL" >/dev/null
check "budget raised by the approved amount" "$((BEFORE + 5))" "$(ho task budget "$TASK" | field 'd["limits"]["agent_launches"]')"
ho task revise "$TASK" "Add subtract and multiply, with tests" >/dev/null
check "requirements versioned" "1" "$(ho task inspect "$TASK" | field 'd["requirements_version"]')"

echo "== duplicate requests and manifests"
DUP="$(ho task create notes "Add a subtract function with tests" | field 'd["key"]')"
check "duplicate waits for the user" "BLOCKED" "$(wait_for BLOCKED task_state "$DUP")"
MANIFEST="$(ho task inspect "$DUP" --manifest)"
check "on-demand manifest records the relationship" "DUPLICATE" \
  "$(field 'd["dag"]["task_relationships"][0]["kind"]' <<<"$MANIFEST")"
check "manifest kind" "ON_DEMAND" "$(field 'd["kind"]' <<<"$MANIFEST")"
check "knowledge list works" "0" "$(ho knowledge list notes | field 'len(d["items"])')"

echo
if [[ $FAILURES -eq 0 ]]; then echo "Phase 7 smoke: all checks passed"; else echo "Phase 7 smoke: $FAILURES check(s) failed"; exit 1; fi
