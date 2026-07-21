#!/bin/sh
set -eu

: "${CUSTOMS_RUNTIME_DB_PASSWORD:?CUSTOMS_RUNTIME_DB_PASSWORD is required}"

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=runtime_password="$CUSTOMS_RUNTIME_DB_PASSWORD" <<'SQL'
SELECT format(
  'CREATE ROLE customs_runtime LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'runtime_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'customs_runtime')
\gexec

ALTER ROLE customs_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE customs TO customs_runtime;
SQL
