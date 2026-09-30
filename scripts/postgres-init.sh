#!/bin/sh
# Runs once, on first PostgreSQL initialization (docker-entrypoint-initdb.d).
# Creates the least-privilege application role used by control-plane.
# The owner role (POSTGRES_USER) runs migrations; ho_app only gets the
# privileges granted in migrations (no UPDATE/DELETE on audit tables).
set -eu
app_password="$(cat /run/secrets/ho_app_db_password)"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_password="$app_password" <<'SQL'
SELECT format('CREATE ROLE ho_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') \gexec
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO ho_app;
SQL
