#!/bin/sh
set -eu

: "${PGHOST:?PGHOST is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"
: "${CUSTOMS_RUNTIME_DB_PASSWORD:?CUSTOMS_RUNTIME_DB_PASSWORD is required}"

# This one-shot deployment step runs for both fresh and pre-existing volumes.
# It therefore rotates the runtime credential on every reviewed deployment,
# unlike /docker-entrypoint-initdb.d which only runs on first initialization.
psql --set=ON_ERROR_STOP=1 \
  --set=runtime_password="$CUSTOMS_RUNTIME_DB_PASSWORD" <<'SQL'
SELECT format(
  'CREATE ROLE customs_runtime LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'runtime_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'customs_runtime')
\gexec

ALTER ROLE customs_runtime LOGIN PASSWORD :'runtime_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE customs TO customs_runtime;
SQL
