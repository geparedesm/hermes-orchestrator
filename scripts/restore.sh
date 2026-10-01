#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh (docs/operations.md).
#
# Verifies the checksums, stops the services that act on state, restores PostgreSQL (in place, with the
# same roles), Hermes's state, and the artifacts, then starts the stack: hermes-init applies the secrets
# from ./secrets again, and the control plane's startup reconciliation brings executions, workspaces, and
# leases back in line with what exists (docs/recovery.md). Credentials are not part of a backup: log in
# again if their volumes were lost (make auth-claude, make auth-codex, make auth-github).
#
# Usage: scripts/restore.sh backups/<timestamp>     (HO_COMPOSE_PROJECT / HO_ENV_FILE as for backup.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP="${1:?usage: scripts/restore.sh <backup directory>}"
BACKUP="$(cd "$BACKUP" && pwd)"
PROJECT="${HO_COMPOSE_PROJECT:-hermes-orchestrator}"
ENV_FILE="${HO_ENV_FILE:-.env}"
HERMES_IMAGE="nousresearch/hermes-agent@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7"
dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }

echo "verifying $BACKUP"
(cd "$BACKUP" && shasum -a 256 -c SHA256SUMS --quiet)

echo "stopping services that change state"
dc stop hermes control-plane >/dev/null 2>&1 || true
dc up -d --wait postgres >/dev/null

echo "restoring PostgreSQL"
dc exec -T postgres pg_restore -U ho_owner -d ho --clean --if-exists --no-owner --role=ho_owner --exit-on-error \
  < "$BACKUP/postgres.dump"
# Brings an older backup up to the current schema (a no-op when the versions match).
dc run --rm -T migrate >/dev/null

echo "restoring Hermes state"
docker run --rm --network none -u 0 --entrypoint /opt/hermes/.venv/bin/python \
  -v "${PROJECT}_hermes-data:/data" -v "$PWD/scripts/hermes_state.py:/opt/ho/hermes_state.py:ro" -v "$BACKUP:/in:ro" \
  "$HERMES_IMAGE" /opt/ho/hermes_state.py import /in/hermes-data.tar.gz

if [[ -f "$BACKUP/artifacts.tar.gz" ]]; then
  echo "restoring artifacts"
  docker run --rm --network none -u 0 --entrypoint sh -v "${PROJECT}_artifacts:/artifacts" -v "$BACKUP:/in:ro" \
    "hermes-orchestrator/control-plane:$(grep -E '^HO_VERSION=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 || echo dev)" \
    -c 'find /artifacts -mindepth 1 -delete && tar -xzf /in/artifacts.tar.gz -C /artifacts && chown -R 10001:10001 /artifacts'
fi

echo "starting the platform"
dc up -d --wait >/dev/null
dc exec -T control-plane ho health >/dev/null
echo "restore complete from $BACKUP"
