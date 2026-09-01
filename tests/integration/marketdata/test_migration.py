"""The append-only guarantee must be the database's, not the code's (I-17)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import pytest
from pydantic import SecretStr

from trading_house.database.connection import open_runtime_connection

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

pytestmark = pytest.mark.integration


def test_marketdata_schema_and_tables_exist(database: DatabaseHarness) -> None:
    with psycopg.connect(database.runtime_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('marketdata.bars'), to_regclass('marketdata.ingest_runs')"
        )
        assert cursor.fetchone() == ("marketdata.bars", "marketdata.ingest_runs")


def test_runtime_has_only_select_and_insert_on_bars_and_ingest_runs(
    database: DatabaseHarness,
) -> None:
    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT "
            "has_table_privilege(current_user, 'marketdata.bars', 'SELECT'), "
            "has_table_privilege(current_user, 'marketdata.bars', 'INSERT'), "
            "has_table_privilege(current_user, 'marketdata.bars', 'UPDATE'), "
            "has_table_privilege(current_user, 'marketdata.bars', 'DELETE'), "
            "has_table_privilege(current_user, 'marketdata.bars', 'TRUNCATE'), "
            "has_table_privilege(current_user, 'marketdata.ingest_runs', 'SELECT'), "
            "has_table_privilege(current_user, 'marketdata.ingest_runs', 'INSERT'), "
            "has_table_privilege(current_user, 'marketdata.ingest_runs', 'UPDATE'), "
            "has_table_privilege(current_user, 'marketdata.ingest_runs', 'DELETE')"
        )
        assert cursor.fetchone() == (
            True,
            True,
            False,
            False,
            False,
            True,
            True,
            False,
            False,
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE marketdata.bars SET quality = quality",
        "DELETE FROM marketdata.bars",
    ],
)
def test_the_runtime_role_cannot_update_or_delete_a_bar(
    database: DatabaseHarness, statement: str
) -> None:
    """I-17 at the database boundary: append-only is a GRANT, not a trigger.

    The runtime role never received UPDATE or DELETE on marketdata.bars, so
    Postgres refuses the statement at the privilege check before it ever looks
    for a matching row -- no row needs to exist for this to fire.
    """

    with open_runtime_connection(SecretStr(database.runtime_dsn)) as connection:
        with (
            connection.cursor() as cursor,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cursor.execute(statement)
        connection.rollback()
