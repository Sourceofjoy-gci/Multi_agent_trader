"""The append-only guarantee must be the database's, not the code's (I-17)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr

from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.models import BarQuality, IngestOutcome

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


def test_public_has_no_privileges_on_either_table(database: DatabaseHarness) -> None:
    """The tables REVOKE ALL FROM PUBLIC explicitly, rather than relying on the
    (also-true) default that a fresh table grants PUBLIC nothing."""

    with psycopg.connect(database.admin_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT "
            "has_table_privilege(0::oid, 'marketdata.bars', 'SELECT'), "
            "has_table_privilege(0::oid, 'marketdata.bars', 'INSERT'), "
            "has_table_privilege(0::oid, 'marketdata.ingest_runs', 'SELECT'), "
            "has_table_privilege(0::oid, 'marketdata.ingest_runs', 'INSERT')"
        )
        assert cursor.fetchone() == (False, False, False, False)


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


def _quoted_literals(constraint_def: str) -> set[str]:
    return set(re.findall(r"'([^']*)'", constraint_def))


def test_the_database_enum_checks_match_the_python_enums(database: DatabaseHarness) -> None:
    """A CHECK listing members by hand drifts the moment someone adds one.
    Read the constraint back and compare it to the enum it is meant to mirror,
    so the drift fails here rather than on a production insert."""

    with psycopg.connect(database.admin_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_get_constraintdef(oid) "
            "FROM pg_catalog.pg_constraint "
            "WHERE conrelid = 'marketdata.bars'::regclass AND conname = 'bars_quality_known'"
        )
        bars_row = cursor.fetchone()
        cursor.execute(
            "SELECT pg_catalog.pg_get_constraintdef(oid) "
            "FROM pg_catalog.pg_constraint "
            "WHERE conrelid = 'marketdata.ingest_runs'::regclass "
            "AND conname = 'runs_outcome_known'"
        )
        runs_row = cursor.fetchone()

    assert bars_row is not None
    assert runs_row is not None
    assert _quoted_literals(bars_row[0]) == {member.value for member in BarQuality}
    assert _quoted_literals(runs_row[0]) == {member.value for member in IngestOutcome}


def test_inserting_a_bar_with_an_unknown_quality_value_is_refused(
    database: DatabaseHarness,
) -> None:
    """The CHECK is proven live, not merely present."""

    run_id = uuid4()
    with open_runtime_connection(SecretStr(database.runtime_dsn)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO marketdata.ingest_runs ("
                "run_id, instrument_id, timeframe, requested_from, requested_to, "
                "started_at, finished_at, bars_returned, bars_stored, bars_rejected, "
                "bars_conflicting, expected_bars, coverage_ratio, outcome"
                ") VALUES (%s, 'fx.eurusd', 'M1', "
                "'2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z', "
                "'2024-01-01T00:00:00Z', '2024-01-01T00:01:00Z', "
                "0, 0, 0, 0, 0, 1.0, 'COMPLETE')",
                (run_id,),
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                cursor.execute(
                    "INSERT INTO marketdata.bars ("
                    "instrument_id, timeframe, event_time, availability_time, "
                    "open, high, low, close, tick_volume, spread, real_volume, "
                    "quality, ingest_run_id"
                    ") VALUES ("
                    "'fx.eurusd', 'M1', '2024-01-01T00:00:00Z', "
                    "'2024-01-01T00:01:00Z', "
                    "1.1, 1.2, 1.0, 1.15, 100, 1, 100, 'NOT_A_REAL_VALUE', %s)",
                    (run_id,),
                )
        connection.rollback()
