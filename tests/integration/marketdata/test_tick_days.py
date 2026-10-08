from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import psycopg
import pytest
from pydantic import SecretStr

from trading_house.core.errors import EvidenceIntegrityError
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.tick_store import PostgresTickDayStore, TickDay, TickDayOutcome

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

pytestmark = pytest.mark.integration

DAY = date(2026, 10, 6)
FETCHED = datetime(2026, 10, 7, 1, tzinfo=UTC)


def _row(outcome: TickDayOutcome, *, day: date = DAY) -> TickDay:
    if outcome is TickDayOutcome.COMPLETE:
        return TickDay(
            instrument_id="fx.eurusd",
            day=day,
            outcome=outcome,
            tick_count=3,
            first_time_ms=1,
            last_time_ms=9,
            crossed_quotes=1,
            point_size=Decimal("0.00001"),
            file_sha256="b" * 64,
            fetched_at=FETCHED,
        )
    if outcome is TickDayOutcome.EMPTY:
        return TickDay(
            instrument_id="fx.eurusd",
            day=day,
            outcome=outcome,
            tick_count=0,
            point_size=Decimal("0.00001"),
            fetched_at=FETCHED,
        )
    return TickDay(
        instrument_id="fx.eurusd",
        day=day,
        outcome=outcome,
        tick_count=0,
        point_size=Decimal("0.00001"),
        detail="BrokerUnavailableError",
        fetched_at=FETCHED,
    )


@pytest.fixture
def tick_store(database: DatabaseHarness) -> Iterator[PostgresTickDayStore]:
    yield PostgresTickDayStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    # The triggers refuse DELETE and TRUNCATE for every role; replica mode is how a
    # superuser test harness clears an append-only table between tests.
    with psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection:
        connection.execute("SET session_replication_role = replica")
        connection.execute("DELETE FROM marketdata.tick_days")


def test_a_recorded_day_reads_back(tick_store: PostgresTickDayStore) -> None:
    tick_store.record(_row(TickDayOutcome.COMPLETE))
    assert tick_store.rows("fx.eurusd") == (_row(TickDayOutcome.COMPLETE),)


def test_a_settled_day_cannot_be_recorded_twice(tick_store: PostgresTickDayStore) -> None:
    tick_store.record(_row(TickDayOutcome.EMPTY))
    with pytest.raises(EvidenceIntegrityError):
        tick_store.record(_row(TickDayOutcome.COMPLETE))


def test_failures_do_not_block_a_later_settlement(tick_store: PostgresTickDayStore) -> None:
    tick_store.record(_row(TickDayOutcome.FAILED))
    tick_store.record(_row(TickDayOutcome.FAILED))
    tick_store.record(_row(TickDayOutcome.COMPLETE))
    assert [row.outcome for row in tick_store.rows("fx.eurusd")] == [
        TickDayOutcome.FAILED,
        TickDayOutcome.FAILED,
        TickDayOutcome.COMPLETE,
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE marketdata.tick_days SET tick_count = 4",
        "DELETE FROM marketdata.tick_days",
        "TRUNCATE marketdata.tick_days",
    ],
)
def test_the_runtime_cannot_rewrite_history(
    tick_store: PostgresTickDayStore, database: DatabaseHarness, statement: str
) -> None:
    tick_store.record(_row(TickDayOutcome.COMPLETE))
    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        pytest.raises(psycopg.Error),
    ):
        connection.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE marketdata.tick_days SET tick_count = 4",
        "DELETE FROM marketdata.tick_days",
        "TRUNCATE marketdata.tick_days",
    ],
)
def test_the_triggers_refuse_even_a_superuser(
    tick_store: PostgresTickDayStore, database: DatabaseHarness, statement: str
) -> None:
    tick_store.record(_row(TickDayOutcome.COMPLETE))
    with (
        psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"),
    ):
        connection.execute(statement)
