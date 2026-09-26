#!/bin/sh
set -eu

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=migration_password="$TRADING_HOUSE_MIGRATION_PASSWORD" \
  --set=runtime_password="$TRADING_HOUSE_RUNTIME_PASSWORD" \
  --set=research_database="$TRADING_HOUSE_RESEARCH_DATABASE" <<'SQL'
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

-- The trial ledger's own database. Every line below names it through the
-- :research_database psql variable rather than a literal, so the CREATE
-- DATABASE, the three grants and the timezone cannot disagree about which
-- database this is -- a grant written for a name nobody created is a grant to
-- nothing, and it would surface as a migration that cannot run. The variable is
-- substituted as a bare identifier, so a name needing SQL quoting is an
-- initialisation error rather than a silent mismatch; compose.yaml fixes it to
-- a plain lowercase name.
SELECT pg_catalog.format('CREATE DATABASE %I', :'research_database')
WHERE NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_database WHERE datname = :'research_database'
)
\gexec
GRANT CONNECT, CREATE ON DATABASE :research_database TO trading_house_owner;
GRANT CONNECT ON DATABASE :research_database TO trading_house_runtime;
-- The same runtime role holds SELECT on the ledger in both databases; what keeps
-- the chain append-only is the absence of every write privilege on the two
-- ledger tables, not the database boundary.
-- The migrator is named explicitly rather than left on the default PUBLIC grant
-- because it is the role that has to reach this database to migrate it.
GRANT CONNECT ON DATABASE :research_database TO trading_house_migrator;
ALTER DATABASE :research_database SET TIME ZONE 'UTC';
SQL

# Alembic's version table lands in `public`, and a fresh database's `public`
# schema grants nothing to the roles this script creates. The grant above covers
# the application database; this one has to be issued *inside* the research
# database, which is why it is a second psql call rather than another line in
# the block above. Without it, `alembic upgrade head` against
# trading_house_research cannot create its version table.
psql \
  --username "$POSTGRES_USER" \
  --dbname "$TRADING_HOUSE_RESEARCH_DATABASE" \
  --set=ON_ERROR_STOP=1 <<'SQL'
GRANT CREATE ON SCHEMA public TO trading_house_owner;
SQL
