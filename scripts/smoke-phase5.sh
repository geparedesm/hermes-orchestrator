#!/usr/bin/env bash
# Phase 5 end-to-end smoke test on a throwaway Compose stack with a local-only repository:
# isolated workspaces, a worker that commits but cannot push, human change detection,
# integration with retest in a runner, a stale merge approval being invalidated, an
# approved merge with post-merge verification, and a forged merge authorization refused.
#
# Usage: scripts/smoke-phase5.sh   (needs Docker, ./secrets, and `make images`)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ho-smoke5
IDENTITY=smoke5
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ho-smoke5.XXXXXX")"
ROOT="$WORK/HermesProjects"
REPO="$ROOT/shop"
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
wait_for() {  # wait_for <expected> <command...> ; prints the last value
  local expected=$1 value=""
  shift
  for _ in $(seq 1 60); do
    value="$("$@" 2>/dev/null || true)"
    [[ "$value" == "$expected" ]] && break
    sleep 2
  done
  echo "$value"
}
task_state() { ho task show "$1" | field 'd["state"]'; }
git_field() { ho git status "$1" | field "$2"; }
events_have() { ho task events "$1" | field "'$2' in {e['type'] for e in d['events']}"; }
gate_pass() {  # gate_pass <task>: stand-in review of the integrated commit, then the Quality Gate (Phase 6)
  local commit
  commit="$(git_field "$1" 'd["changes"]["integration_sha"]')"
  sql "INSERT INTO reviews (id, task_id, execution_id, commit_sha, reviewer_provider, developer_providers, outcome, requirements_met, summary)
       SELECT gen_random_uuid(), t.id, e.id, '$commit', 'claude', '{}', 'APPROVED', true, 'smoke stand-in review'
       FROM tasks t JOIN executions e ON e.task_id = t.id WHERE t.key = '$1' ORDER BY e.created_at DESC LIMIT 1" >/dev/null
  sql "UPDATE tasks SET state = 'QUALITY_GATE', resume_state = NULL WHERE key = '$1'" >/dev/null  # Phase 7 stand-in
  ho gate evaluate "$1" | field 'd["outcome"]'
}
cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    docker volume rm "cred-codex-$IDENTITY" >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

echo "== setup"
mkdir -p "$REPO/.hermes" "$REPO/tests" "$WORK/project-secrets"
cat > "$REPO/.hermes/project.yaml" <<'YAML'
version: 1
project: {name: shop}
toolchain: {profiles: [python]}
agents: {allowed_providers: [claude, codex]}
commands: {test: "python -m unittest -v"}
YAML
{ printf '"""Shop pricing."""\n\nCURRENCY = "USD"\n'; for i in $(seq 1 12); do printf '\n\ndef rule_%s(amount):\n    return amount\n' "$i"; done
  printf '\n\ndef price(amount):\n    return round(amount, 2)\n'; } > "$REPO/shop.py"
printf 'import unittest\nfrom shop import price\n\n\nclass T(unittest.TestCase):\n    def test_price(self):\n        self.assertEqual(price(1.234), 1.23)\n' > "$REPO/tests/test_shop.py"
touch "$REPO/tests/__init__.py"
printf '# shop\n' > "$REPO/README.md"
git -C "$REPO" init -q -b main && git -C "$REPO" add -A && git -C "$REPO" commit -qm "initial shop"
printf 'HO_MACHINE_PROFILE=mac-m2-pro\nHO_PROJECTS_ROOT_HOST=%s\nHO_VERSION=dev\nHO_PROVIDER_IDENTITY=%s\nHO_PROJECT_SECRETS_HOST=%s\n' \
  "$ROOT" "$IDENTITY" "$WORK/project-secrets" > "$ENV_FILE"
echo "HO_HERMES_DASHBOARD_PORT=19205" >> "$ENV_FILE"  # never collide with the operator stack's Hermes
docker volume create --label "ho.credential=codex/$IDENTITY" "cred-codex-$IDENTITY" >/dev/null
dc up -d --build --wait >/dev/null
ho project register "$REPO" >/dev/null
APPROVAL="$(ho project scan shop | field 'd["approval"]["id"]')"
ho approval approve "$APPROVAL" >/dev/null
TASK="$(ho task create shop "Add a discount function" | field 'd["key"]')"
check "task READY" "READY" "$(wait_for READY task_state "$TASK")"

echo "== isolated workspace; the worker commits but cannot push"
WS="$(ho git workspace "$TASK" | field 'd["path"]')"
check "workspace inside .hermes/worktrees" "True" "$([[ $WS == .hermes/worktrees/* ]] && echo True || echo False)"
EXEC="$(ho execution run "$TASK" '
  printf "\ndef discount(amount, percent):\n    return price(amount * (100 - percent) / 100)\n" >> shop.py
  printf "\n    def test_discount(self):\n        from shop import discount\n        self.assertEqual(discount(10, 25), 7.5)\n" >> tests/test_shop.py
  git add -A && git commit -qm "Add discount" && echo committed
  git remote | wc -l | xargs echo remotes=
  git push 2>&1 | head -1' --role DEVELOPER --provider codex --workspace "$WS" --workspace-access WRITE | field 'd["id"]')"
check "worker finished" "SUCCEEDED" "$(wait_for SUCCEEDED sh -c "docker compose -p $PROJECT --env-file $ENV_FILE exec -T control-plane ho execution show $EXEC | python3 -c \"import json,sys; print(json.load(sys.stdin)['state'])\"")"
check "user checkout untouched" "initial shop|" "$(git -C "$REPO" log -1 --format=%s)|$(git -C "$REPO" status --porcelain)"
check "worker had no remote to push to" "1" "$(dc exec -T control-plane sh -c "cat /var/lib/ho/artifacts/*/*/executions/$EXEC/*logs.txt" | grep -c 'remotes= 0' || true)"

echo "== human changes while the task works"
printf '# shop\n\nMaintained by the user.\n' > "$REPO/README.md"
git -C "$REPO" commit -qam "User updates the README"
check "different file: LOW" "LOW" "$(ho git divergence "$TASK" | field 'd["level"]')"
sed -i.bak 's/^CURRENCY = "USD"$/CURRENCY = "EUR"/' "$REPO/shop.py" && rm -f "$REPO/shop.py.bak"
git -C "$REPO" commit -qam "User changes the currency"
check "same file, different area: MEDIUM" "MEDIUM" "$(ho git divergence "$TASK" | field 'd["level"]')"
check "HUMAN_CHANGE_DETECTED recorded" "True" "$(events_have "$TASK" HUMAN_CHANGE_DETECTED)"

echo "== integration onto the current main, retested in a runner"
check "integration succeeded" "True" "$(ho git integrate "$TASK" | field 'd["ok"]')"
check "retest passed" "PASSED" "$(wait_for PASSED git_field "$TASK" 'd["changes"]["retest_status"]')"
check "main still the user's" "User changes the currency" "$(git -C "$REPO" log -1 --format=%s)"

echo "== a merge approval is bound to the exact commits"
check "Quality Gate passes" "PASS" "$(gate_pass "$TASK")"
STALE="$(ho git merge-request "$TASK" | field 'd["id"]')"
printf '# shop\n\nMaintained by the user. Updated again.\n' > "$REPO/README.md"
git -C "$REPO" commit -qam "User commits after the merge request"
ho approval approve "$STALE" >/dev/null || true
check "stale approval invalidated" "INVALIDATED" "$(ho approval show "$STALE" | field 'd["state"]')"
check "task back to RUNNING, nothing merged" "RUNNING|User commits after the merge request" \
  "$(task_state "$TASK")|$(git -C "$REPO" log -1 --format=%s)"

echo "== forged merge authorization refused by Git Service"
FORGED="$(dc exec -T control-plane python -c "
import httpx, json
token = open('/run/secrets/ho_git_service_token').read().strip()
auth = {'approval_id': 'forged', 'expires_at': '2099-01-01T00:00:00+00:00', 'signature': '0' * 64,
        'subject': {'project': 'shop', 'target_branch': 'main', 'target_sha': '0' * 40, 'head_sha': '1' * 40,
                    'method': 'merge', 'pr_number': None}}
r = httpx.post('http://git-service:8081/v1/merge', json={'path': 'shop', 'task': '$TASK', 'authorization': auth},
               headers={'Authorization': 'Bearer ' + token})
print(r.status_code)")"
check "forged authorization: 403" "403" "$FORGED"

echo "== reintegrate, approve, merge, verify"
ho git integrate "$TASK" >/dev/null
check "retest passed again" "PASSED" "$(wait_for PASSED git_field "$TASK" 'd["changes"]["retest_status"]')"
check "Quality Gate passes again" "PASS" "$(gate_pass "$TASK")"
printf 'draft\n' > "$REPO/notes.txt"  # the user's uncommitted, unrelated work
MERGE="$(ho git merge-request "$TASK" | field 'd["id"]')"
ho approval approve "$MERGE" >/dev/null
check "task DONE after post-merge tests" "DONE" "$(wait_for DONE task_state "$TASK")"
check "merge commit on main" "Merge $TASK (merge, approval $MERGE)" "$(git -C "$REPO" log -1 --format=%s)"
check "feature in the user's checkout" "1" "$(grep -c 'def discount' "$REPO/shop.py")"
check "user's uncommitted work kept" "draft" "$(cat "$REPO/notes.txt")"
check "post-merge verification recorded" "PASSED" "$(git_field "$TASK" 'd["changes"]["post_merge_status"]')"
check "workspaces removed" "0" "$(ls "$REPO/.hermes/worktrees" 2>/dev/null | wc -l | tr -d ' ')"
check "merge audited" "True" "$(events_have "$TASK" MERGE_COMPLETED)"

if [[ "$FAILURES" -ne 0 ]]; then
  echo "$FAILURES check(s) failed"
  exit 1
fi
echo "all checks passed"
