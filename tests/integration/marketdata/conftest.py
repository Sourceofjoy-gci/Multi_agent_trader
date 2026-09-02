"""Shared market-data test doubles and helpers.

Lives here, not in either test module, because pytest runs this tree under
``--import-mode=importlib`` with no ``__init__.py`` anywhere underneath
``tests/`` -- importing a name out of a sibling test module by its bare
module name is unreliable under that mode, exactly as it was for
``FakeTerminal`` in Phase 1 (see ``tests/unit/brokers/mt5/conftest.py``). A
conftest is always discovered by pytest itself, and its symbols can be
reached with an absolute, rootdir-relative import
(``tests.integration.marketdata.conftest``) from any test module that needs
them -- including the write-path and read-path test modules, which need the
exact same seeded fixture to agree on what is in the store.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import SecretStr

from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.models import Bar, BarQuality, IngestOutcome, IngestRun, Timeframe
from trading_house.marketdata.store import PostgresBarStore, WriteResult

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

NINE = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
"""The fixed instant every test bar's minute offset is measured from."""

_INSTRUMENT_ID = "fx.eurusd"


def _bar(minute: int, quality: BarQuality = BarQuality.OK) -> Bar:
    """A coherent bar opening ``minute`` minutes after ``NINE``.

    ``quality`` only ever tags the bar; the OHLC stays internally coherent
    regardless, because a defective *quality* label and an incoherent
    *price* are two different things this module needs to test separately.
    """

    event_time = NINE + timedelta(minutes=minute)
    return Bar(
        instrument_id=_INSTRUMENT_ID,
        timeframe=Timeframe.M1,
        event_time=event_time,
        availability_time=event_time + timedelta(minutes=1),
        open=Decimal("1.10000"),
        high=Decimal("1.10030"),
        low=Decimal("1.09990"),
        close=Decimal("1.10020"),
        tick_volume=100,
        spread=1,
        real_volume=0,
        quality=quality,
    )


def seed(
    store: PostgresBarStore, bars: Sequence[Bar], *, run_id: UUID | None = None
) -> WriteResult:
    """Record a minimal valid run, append ``bars`` against it, finalize the
    run with what the write actually did, and return the write result.

    ``run_id`` can be supplied so a caller that needs to read the run row
    back afterward knows which one to look for.
    """

    run = IngestRun(
        run_id=run_id or uuid4(),
        instrument_id=_INSTRUMENT_ID,
        timeframe=Timeframe.M1,
        requested_from=NINE,
        requested_to=NINE + timedelta(hours=1),
        started_at=NINE,
        finished_at=NINE + timedelta(hours=1),
        earliest_event_time=None,
        bars_returned=len(bars),
        bars_stored=0,
        bars_rejected=0,
        bars_conflicting=0,
        expected_bars=len(bars),
        coverage_ratio=Decimal("1"),
        outcome=IngestOutcome.COMPLETE,
        detail=None,
    )
    store.record_run(run)
    result = store.append_bars(bars, run_id=run.run_id)
    store.finalize_run(
        run.run_id,
        bars_stored=result.stored,
        bars_conflicting=result.conflicting,
        outcome=run.outcome,
        finished_at=run.finished_at,
    )
    return result


@pytest.fixture
def bar_store(database: DatabaseHarness) -> Iterator[PostgresBarStore]:
    """A ``PostgresBarStore`` against the integration harness, emptied after use."""

    store = PostgresBarStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    yield store
    with (
        psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("TRUNCATE marketdata.bars, marketdata.ingest_runs")
