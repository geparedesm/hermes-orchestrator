#!/bin/sh
# Create random service passwords and tokens in ./secrets (never committed).
# Existing files are kept, so this is safe to rerun.
set -eu
cd "$(dirname "$0")/.."
umask 077
mkdir -p secrets
for name in ho_owner_db_password ho_app_db_password ho_redis_password ho_plugin_token ho_operator_token ho_git_service_token; do
  if [ ! -s "secrets/$name" ]; then
    openssl rand -hex 32 > "secrets/$name"
    echo "created secrets/$name"
  fi
done
# Containers run as UID 10001 and must be able to read the mounted files.
# Docker Compose file secrets are bind mounts, so relax to 0644 only inside the
# owner-only secrets/ directory (mode 0700).
chmod 700 secrets
chmod 644 secrets/ho_*
