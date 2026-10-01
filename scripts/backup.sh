#!/usr/bin/env bash
# Critical-state backup (MASTER_SPEC section 79; docs/operations.md).
#
# Backs up: PostgreSQL (all platform state), Hermes's state (consistent SQLite copies, secrets removed),
# the platform configuration, and the artifacts volume (evidence; skip with HO_BACKUP_ARTIFACTS=0).
# Never backs up: ./secrets, provider credential volumes (cred-*), the GitHub login (gh-config), Redis,
# workers, or test environments. Projects themselves live in Git (and their own backups).
#
# Usage: scripts/backup.sh                     # uses .env and the default Compose project
#        HO_BACKUP_DIR=/path HO_BACKUP_KEEP=14 HO_COMPOSE_PROJECT=name HO_ENV_FILE=file scripts/backup.sh
# Daily: see docs/operations.md (launchd on macOS, cron or a systemd timer on Linux).
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${HO_COMPOSE_PROJECT:-hermes-orchestrator}"
ENV_FILE="${HO_ENV_FILE:-.env}"
DIR="${HO_BACKUP_DIR:-./backups}"
KEEP="${HO_BACKUP_KEEP:-14}"
HERMES_IMAGE="nousresearch/hermes-agent@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7"

dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
umask 077
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DIR/$STAMP"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
trap 'echo "backup failed; removing the incomplete $OUT" >&2; rm -rf "$OUT"' ERR

echo "backup $STAMP -> $OUT"
dc exec -T postgres pg_dump -U ho_owner -d ho --format=custom --no-password > "$OUT/postgres.dump"
echo "  postgres     $(du -h "$OUT/postgres.dump" | cut -f1)"

docker run --rm --network none -u 0 --entrypoint /opt/hermes/.venv/bin/python \
  -v "${PROJECT}_hermes-data:/data:ro" -v "$PWD/scripts/hermes_state.py:/opt/ho/hermes_state.py:ro" -v "$OUT:/out" \
  "$HERMES_IMAGE" /opt/ho/hermes_state.py export /out/hermes-data.tar.gz
echo "  hermes       $(du -h "$OUT/hermes-data.tar.gz" | cut -f1)"

if [[ "${HO_BACKUP_ARTIFACTS:-1}" == "1" ]]; then
  docker run --rm --network none -u 0 --entrypoint tar -v "${PROJECT}_artifacts:/artifacts:ro" -v "$OUT:/out" \
    "hermes-orchestrator/control-plane:$(grep -E '^HO_VERSION=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 || echo dev)" \
    -czf /out/artifacts.tar.gz -C /artifacts .
  echo "  artifacts    $(du -h "$OUT/artifacts.tar.gz" | cut -f1)"
fi

mkdir -p "$OUT/config"
cp -R config/. "$OUT/config/"
if [[ -f "$ENV_FILE" ]]; then  # settings only: values of keys that look like credentials are dropped
  grep -vE '^[A-Z0-9_]*(TOKEN|PASSWORD|SECRET|KEY)[A-Z0-9_]*=' "$ENV_FILE" > "$OUT/env.settings" || true
fi

{
  echo "{"
  echo "  \"created_at\": \"$STAMP\","
  echo "  \"git_commit\": \"$(git rev-parse HEAD 2>/dev/null || echo unknown)\","
  echo "  \"ho_version\": \"$(grep -E '^HO_VERSION=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 || echo dev)\","
  echo "  \"alembic_revision\": \"$(dc exec -T postgres psql -U ho_owner -d ho -tAc 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')\""
  echo "}"
} > "$OUT/MANIFEST.json"
(cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 shasum -a 256 > SHA256SUMS)
trap - ERR

# Keep the newest $KEEP backups.
ls -1d "$DIR"/[0-9]*T*Z 2>/dev/null | sort | head -n "-$KEEP" | while read -r old; do rm -rf "$old"; echo "  pruned       $old"; done
echo "backup complete: $OUT"
