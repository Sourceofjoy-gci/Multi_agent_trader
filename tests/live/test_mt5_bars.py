"""Live tests: real bars through Mt5BrokerAdapter, and a real backfill into
Postgres, both needing a demo terminal.

Marked ``mt5`` and skipped when no suitable terminal is present, so a bare
``uv run pytest`` stays green on any machine. Never run in CI.

The ``skip_reason()`` probe is imported from the shared ``conftest.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from pydantic import SecretStr

from trading_house.brokers.mt5.boundary import TerminalPort
from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.constitution.binding import parse_venue_binding
from trading_house.core.clock import SystemClock
from trading_house.marketdata.models import IngestOutcome, Timeframe, duration

from .conftest import skip_reason

if TYPE_CHECKING:
    from ..conftest import DatabaseHarness

PROBE_SYMBOL = "EURUSD"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

BINDING = parse_venue_binding(
    b"""
venue: mt5
books:
  fx_scalp: {magic_range: [110000, 119999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD"}
"""
)


_SKIP = skip_reason()

pytestmark = [
    pytest.mark.mt5,
    pytest.mark.skipif(_SKIP is not None, reason=_SKIP or ""),
]


def _terminal() -> TerminalPort:
    """Imported here, not at module scope: terminal.py cannot be imported at
    all off Windows, and this module must still collect there to be skipped."""

    from trading_house.brokers.mt5.terminal import Mt5Terminal

    return Mt5Terminal()


def test_a_real_window_of_h1_bars_satisfies_utc_and_ohlc_guarantees() -> None:
    """The whole point of this phase: prove the timestamp and OHLC-ordering
    guarantees against a real broker's numbers, not just synthetic fixtures.

    ``Mt5BrokerAdapter.history`` returns ``Mt5Bar`` -- the boundary DTO, not
    the canonical ``Bar`` -- so this checks the two properties that are true
    at that boundary before ``quality.assess`` and ``Bar`` construction ever
    happen: the timestamp conversion is real UTC, and a real broker's OHLC
    values are never internally contradictory.
    """

    from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter

    end = datetime.now(UTC)
    start = end - timedelta(days=10)

    with Mt5Gateway(_terminal(), clock=SystemClock()) as gateway:
        adapter = Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock())
        bars = adapter.history("fx.eurusd", Timeframe.H1, start, end)

    assert len(bars) > 0, "expected at least one closed H1 bar in the last 10 days"
    for bar in bars:
        assert bar.event_time.tzinfo is not None
        assert bar.event_time.utcoffset() == timedelta(0)
        assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        assert bar.low > 0


def test_a_known_m15_window_comes_back_as_exactly_that_window() -> None:
    """Settles the window semantics against a real broker: two hours of M15
    on last Wednesday, mid-session, must come back as exactly its eight bars.
    Unshifted, a UTC+3 broker answers with 07:00-09:00 instead of 10:00-12:00;
    end-inclusive, it adds a ninth bar at 12:00."""

    from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    wednesday = today - timedelta(days=(today.weekday() - 2) % 7 or 7)
    start = wednesday + timedelta(hours=10)
    end = start + timedelta(hours=2)

    with Mt5Gateway(_terminal(), clock=SystemClock()) as gateway:
        adapter = Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock())
        bars = adapter.history("fx.eurusd", Timeframe.M15, start, end)

    assert [bar.event_time for bar in bars] == [start + timedelta(minutes=15 * i) for i in range(8)]


def _delete_test_rows(dsn: str, *, instrument_id: str, timeframe: Timeframe) -> None:
    """Remove only this test's own rows, never a blanket TRUNCATE -- other
    tests sharing the session-scoped database fixture may have state of
    their own in flight."""

    with (
        psycopg.connect(dsn, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "DELETE FROM marketdata.bars WHERE instrument_id = %s AND timeframe = %s",
            (instrument_id, timeframe.value),
        )
        cursor.execute(
            "DELETE FROM marketdata.ingest_runs WHERE instrument_id = %s AND timeframe = %s",
            (instrument_id, timeframe.value),
        )


def test_a_real_backfill_round_trips_through_the_store(database: DatabaseHarness) -> None:
    """The brief's actual ask for this step: exercise the whole write path --
    ``assess`` -> ``Bar`` -> ``append_bars`` -> availability-filtered
    ``bars()`` -- against a real broker, not just the boundary DTO the test
    above stops at.

    Fetches a real H1 window twice: once directly through the adapter (the
    reference), pinned to the exact ``(until, requested_to)`` window the
    backfill run itself recorded, and once by driving ``ingest.backfill``
    into a real Postgres store and reading it back through ``bars()``. The
    two must agree bar for bar, and the run's own ``coverage_ratio`` must be
    a plausible fraction rather than nonsense.
    """

    from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter
    from trading_house.brokers.mt5.contracts import decimal_of
    from trading_house.database.connection import open_runtime_connection
    from trading_house.marketdata.ingest import backfill
    from trading_house.marketdata.store import PostgresBarStore

    instrument_id = "fx.eurusd"
    timeframe = Timeframe.H1
    until = datetime.now(UTC) - timedelta(days=10)
    store = PostgresBarStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))

    _delete_test_rows(database.test_superuser_dsn, instrument_id=instrument_id, timeframe=timeframe)
    try:
        with Mt5Gateway(_terminal(), clock=SystemClock()) as gateway:
            adapter = Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock())

            run = backfill(
                adapter,
                store,
                SystemClock(),
                instrument_id=instrument_id,
                timeframe=timeframe,
                until=until,
                server_offset_seconds=gateway.server_utc_offset_seconds,
            )

            assert run.outcome is not IngestOutcome.FAILED, run.detail
            assert run.bars_returned > 0, "expected at least one closed H1 bar in the last 10 days"
            assert Decimal("0") < run.coverage_ratio <= Decimal("1.2"), run.coverage_ratio

            # The exact window backfill actually used, not a freshly computed
            # "now" -- so this reference fetch can never race the one
            # backfill already made.
            reference = adapter.history(instrument_id, timeframe, until, run.requested_to)

        coverage = store.coverage(instrument_id, timeframe)
        assert coverage.earliest_event_time is not None

        # One bar's width of slack past requested_to, so the comparison
        # cannot hinge on how either side treats the boundary bar.
        as_of = run.requested_to + duration(timeframe)
        stored = {
            bar.event_time: bar
            for bar in store.bars(
                instrument_id,
                timeframe,
                start=coverage.earliest_event_time,
                end=as_of,
                as_of=as_of,
                include_defective=True,
            )
        }

        assert len(stored) == len(reference)
        for raw in reference:
            bar = stored[raw.event_time]
            assert bar.open == decimal_of(raw.open)
            assert bar.high == decimal_of(raw.high)
            assert bar.low == decimal_of(raw.low)
            assert bar.close == decimal_of(raw.close)
            assert bar.tick_volume == raw.tick_volume
            assert bar.spread == raw.spread
            assert bar.real_volume == raw.real_volume
    finally:
        _delete_test_rows(
            database.test_superuser_dsn, instrument_id=instrument_id, timeframe=timeframe
        )
