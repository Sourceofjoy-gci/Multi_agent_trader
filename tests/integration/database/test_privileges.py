from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from trading_house.database.connection import open_runtime_connection

if TYPE_CHECKING:
    from ..conftest import DatabaseHarness

pytestmark = pytest.mark.integration


def _append_one(database: DatabaseHarness) -> None:
    event_json = {
        "event_id": str(uuid4()),
        "event_type": "privilege.test",
        "payload": {},
    }
    event_bytes = json.dumps(event_json, sort_keys=True, separators=(",", ":")).encode()
    with open_runtime_connection(SecretStr(database.runtime_dsn)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM audit.append_event(%s, %s)",
                (event_bytes, Jsonb(event_json)),
            )
        connection.commit()


def test_roles_are_distinct_and_migrator_can_set_nonlogin_owner(
    database: DatabaseHarness,
) -> None:
    with psycopg.connect(database.admin_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolname, rolcanlogin, rolsuper FROM pg_catalog.pg_roles "
            "WHERE rolname IN "
            "('trading_house_owner', 'trading_house_migrator', 'trading_house_runtime') "
            "ORDER BY rolname"
        )
        assert cursor.fetchall() == [
            ("trading_house_migrator", True, False),
            ("trading_house_owner", False, False),
            ("trading_house_runtime", True, False),
        ]

    with psycopg.connect(database.migration_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE trading_house_owner")
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("trading_house_owner",)


def test_runtime_has_only_the_required_ledger_and_function_privileges(
    database: DatabaseHarness,
) -> None:
    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT "
            "has_table_privilege(current_user, 'audit.ledger', 'SELECT'), "
            "has_table_privilege(current_user, 'audit.ledger', 'INSERT'), "
            "has_table_privilege(current_user, 'audit.ledger', 'UPDATE'), "
            "has_table_privilege(current_user, 'audit.ledger', 'DELETE'), "
            "has_table_privilege(current_user, 'audit.ledger', 'TRUNCATE'), "
            "has_function_privilege(current_user, "
            "'audit.append_event(bytea,jsonb)', 'EXECUTE'), "
            "has_table_privilege(current_user, 'public.alembic_version', 'SELECT')"
        )
        assert cursor.fetchone() == (True, False, False, False, False, True, True)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO audit.ledger DEFAULT VALUES",
        "UPDATE audit.ledger SET canonical_event = canonical_event",
        "DELETE FROM audit.ledger",
        "TRUNCATE audit.ledger",
        "ALTER TABLE audit.ledger ADD COLUMN forbidden integer",
        "CREATE TABLE audit.forbidden(value integer)",
    ],
)
def test_runtime_sql_mutations_are_actually_rejected(
    database: DatabaseHarness, statement: str
) -> None:
    with open_runtime_connection(SecretStr(database.runtime_dsn)) as connection:
        with (
            connection.cursor() as cursor,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cursor.execute(statement)
        connection.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.ledger SET canonical_event = canonical_event",
        "DELETE FROM audit.ledger",
        "TRUNCATE audit.ledger",
    ],
)
def test_append_only_triggers_reject_owner_mutations(
    database: DatabaseHarness, statement: str
) -> None:
    _append_one(database)
    with psycopg.connect(database.migration_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE trading_house_owner")
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                cursor.execute(statement)
        connection.rollback()


def test_public_privileges_are_revoked_and_security_definer_is_pinned(
    database: DatabaseHarness,
) -> None:
    with psycopg.connect(database.admin_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT "
            "has_schema_privilege(0::oid, 'audit', 'USAGE'), "
            "has_schema_privilege(0::oid, 'audit_crypto', 'USAGE'), "
            "has_table_privilege(0::oid, 'audit.ledger', 'SELECT'), "
            "has_function_privilege(0::oid, "
            "'audit.append_event(bytea,jsonb)', 'EXECUTE')"
        )
        assert cursor.fetchone() == (False, False, False, False)

        cursor.execute(
            "SELECT r.rolname, p.prosecdef, p.proconfig "
            "FROM pg_catalog.pg_proc AS p "
            "JOIN pg_catalog.pg_roles AS r ON r.oid = p.proowner "
            "WHERE p.oid = 'audit.append_event(bytea,jsonb)'::regprocedure"
        )
        assert cursor.fetchone() == (
            "trading_house_owner",
            True,
            ["search_path=pg_catalog"],
        )


def test_owner_default_privileges_deny_public_execution_of_future_functions(
    database: DatabaseHarness,
) -> None:
    try:
        with (
            psycopg.connect(database.migration_dsn) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SET ROLE trading_house_owner")
            cursor.execute(
                "CREATE FUNCTION audit.future_function_probe() "
                "RETURNS INTEGER LANGUAGE SQL AS 'SELECT 1'"
            )

        with (
            psycopg.connect(database.runtime_dsn) as connection,
            connection.cursor() as cursor,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cursor.execute("SELECT audit.future_function_probe()")
    finally:
        with (
            psycopg.connect(database.migration_dsn) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SET ROLE trading_house_owner")
            cursor.execute("DROP FUNCTION IF EXISTS audit.future_function_probe()")
