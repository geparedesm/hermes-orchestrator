#!/usr/bin/env bash
# Health check of a running stack (used after updates, restores, and rollbacks; docs/operations.md).
# Exits non-zero if the control plane is not ready, a dependency is DEGRADED, the database schema is not at
# the code's migration head, or Hermes's plugin and Dashboard are not up.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${HO_COMPOSE_PROJECT:-hermes-orchestrator}"
ENV_FILE="${HO_ENV_FILE:-.env}"
dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
fail() { echo "check failed: $*" >&2; exit 1; }

[[ "${HO_UPDATE_INJECT_FAILURE:-0}" == "1" ]] && fail "failure injected (HO_UPDATE_INJECT_FAILURE=1, used by tests)"
for _ in $(seq 1 30); do
  ready="$(dc exec -T control-plane ho health 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))' 2>/dev/null || true)"
  [[ "$ready" == "ok" ]] && break
  sleep 2
done
[[ "$ready" == "ok" ]] || fail "control plane health is '${ready:-unreachable}'"
state="$(dc exec -T control-plane ho recovery status | python3 -c 'import json,sys; print(json.load(sys.stdin)["health"]["state"])')"
[[ "$state" == "HEALTHY" ]] || fail "a platform dependency is $state"
head="$(dc run --rm -T --no-deps --entrypoint python migrate -c "from alembic.config import Config; from alembic.script import ScriptDirectory
c = Config('/app/migrations/alembic.ini'); c.set_main_option('script_location', '/app/migrations')
print(ScriptDirectory.from_config(c).get_current_head())" 2>/dev/null | tail -1 | tr -d '[:space:]')"
current="$(dc exec -T postgres psql -U ho_owner -d ho -tAc 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')"
[[ -n "$head" && "$head" == "$current" ]] || fail "database schema $current is not the code's head ${head:-?}"
plugins="$(dc exec -T -e HERMES_HOME=/opt/data hermes hermes plugins list 2>/dev/null || true)"
grep -qiE 'orchestration.*enabled' <<<"$plugins" || fail "Hermes's orchestration plugin is not enabled"
port="$(grep -E '^HO_HERMES_DASHBOARD_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port:-9119}/api/auth/providers" || true)"
[[ "$code" == "200" ]] || fail "Hermes Dashboard returned $code"
echo "check passed: control plane ready, dependencies healthy, schema $current, Hermes plugin and Dashboard up"
