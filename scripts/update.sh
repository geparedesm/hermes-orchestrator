#!/usr/bin/env bash
# Approval-controlled platform update (MASTER_SPEC section 79; docs/operations.md):
#   snapshot -> update -> health check -> (rollback/restore on failure)
#
# 1. scripts/update.sh <version>                 requests the UPDATE approval and stops
# 2. approve it: ho approval approve <id>, the Dashboard, or /orch approve <id> (an allowed approver)
# 3. scripts/update.sh <version> <approval id>   consumes the approval, backs up, builds and starts <version>
#    from this checkout, checks health, and rolls back to the backup and the previous version on failure.
set -euo pipefail
cd "$(dirname "$0")/.."
TO="${1:?usage: scripts/update.sh <version> [approval id]}"
APPROVAL="${2:-}"
PROJECT="${HO_COMPOSE_PROJECT:-hermes-orchestrator}"
ENV_FILE="${HO_ENV_FILE:-.env}"
dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
ho() { dc exec -T control-plane ho "$@"; }
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }

if [[ -z "$APPROVAL" ]]; then
  ID="$(ho update request "$TO" | field 'd["id"]')"
  echo "update to $TO requested: approval $ID"
  echo "approve it (ho approval approve $ID, the Dashboard, or /orch approve $ID), then run: scripts/update.sh $TO $ID"
  exit 0
fi

FROM="$(grep -E '^HO_VERSION=' "$ENV_FILE" | cut -d= -f2 || true)"
FROM="${FROM:-dev}"
# Consume the approval first: the snapshot then contains the started update, so its record survives a rollback.
RUN="$(ho update start "$APPROVAL" | field 'd["id"]')"
echo "== snapshot (update $RUN: $FROM -> $TO)"
BACKUP="$(HO_BACKUP_DIR="${HO_BACKUP_DIR:-./backups}" scripts/backup.sh | tail -1 | sed 's/^backup complete: //')"
echo "backup $BACKUP"

echo "== update"
cp "$ENV_FILE" "$ENV_FILE.pre-update"
set_version() { if grep -qE '^HO_VERSION=' "$ENV_FILE"; then sed -i.bak "s/^HO_VERSION=.*/HO_VERSION=$1/" "$ENV_FILE" && rm -f "$ENV_FILE.bak"; else echo "HO_VERSION=$1" >> "$ENV_FILE"; fi; }
if HO_VERSION="$TO" make images >/dev/null && set_version "$TO" && dc up -d --build --wait >/dev/null && scripts/check.sh; then
  ho update finish "$RUN" SUCCEEDED --note "updated from $FROM; backup $BACKUP" >/dev/null
  echo "update to $TO succeeded"
  exit 0
fi

echo "== update failed: rolling back to $FROM" >&2
HO_ROLLBACK_VERSION="$FROM" scripts/rollback.sh "$BACKUP"
ho update finish "$RUN" ROLLED_BACK --note "health check failed after updating to $TO; restored $BACKUP" >/dev/null
echo "rolled back to $FROM" >&2
exit 1
