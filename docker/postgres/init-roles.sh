#!/bin/sh
set -eu

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=migration_password="$TRADING_HOUSE_MIGRATION_PASSWORD" \
  --set=runtime_password="$TRADING_HOUSE_RUNTIME_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE trading_house_owner NOLOGIN'
WHERE NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'trading_house_owner'
)
\gexec

SELECT pg_catalog.format(
  'CREATE ROLE trading_house_migrator LOGIN PASSWORD %L',
  :'migration_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'trading_house_migrator'
)
\gexec

GRANT trading_house_owner TO trading_house_migrator;

SELECT pg_catalog.format(
  'CREATE ROLE trading_house_runtime LOGIN PASSWORD %L',
  :'runtime_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'trading_house_runtime'
)
\gexec

GRANT CONNECT, CREATE ON DATABASE trading_house TO trading_house_owner;
GRANT CREATE ON SCHEMA public TO trading_house_owner;
GRANT CONNECT ON DATABASE trading_house TO trading_house_runtime;
ALTER DATABASE trading_house SET TIME ZONE 'UTC';
SQL
