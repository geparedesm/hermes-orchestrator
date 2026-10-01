#!/usr/bin/env bash
# Return to the previous version and the state saved before an update (docs/operations.md).
# Usage: HO_ROLLBACK_VERSION=<previous version> scripts/rollback.sh <backup directory>
# The previous images are still present (each version has its own tag); state comes from the backup because
# migrations only move forward.
set -euo pipefail
cd "$(dirname "$0")/.."
BACKUP="${1:?usage: HO_ROLLBACK_VERSION=<version> scripts/rollback.sh <backup directory>}"
VERSION="${HO_ROLLBACK_VERSION:?set HO_ROLLBACK_VERSION to the version to return to}"
ENV_FILE="${HO_ENV_FILE:-.env}"
if grep -qE '^HO_VERSION=' "$ENV_FILE"; then sed -i.bak "s/^HO_VERSION=.*/HO_VERSION=$VERSION/" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
else echo "HO_VERSION=$VERSION" >> "$ENV_FILE"; fi
scripts/restore.sh "$BACKUP"
HO_UPDATE_INJECT_FAILURE=0 scripts/check.sh
echo "rolled back to $VERSION with the state from $BACKUP"
