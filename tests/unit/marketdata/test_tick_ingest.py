from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.unit.marketdata.tick_fakes import (
    FakeTickProvider,
    InMemoryTickDayStore,
    day_ticks,
    raw_ticks,
    weekdays_only,
)
from trading_house.core.clock import FixedClock
from trading_house.core.errors import BrokerUnavailableError, ConfigurationError
from trading_house.marketdata.tick_files import day_path, read_day_file
from trading_house.marketdata.tick_ingest import (
    backfill_ticks,
    fetch_day,
    last_closed_day,
    update_ticks,
)
from trading_house.marketdata.tick_store import TickDayOutcome
from trading_house.marketdata.ticks import RawTicks, day_digest

POINT = Decimal("0.00001")
TUESDAY = date(2026, 10, 6)
CLOCK = FixedClock(datetime(2026, 10, 7, 1, 0, tzinfo=UTC))  # Wednesday 01:00 UTC


def _fetch(
    provider: FakeTickProvider, store: InMemoryTickDayStore, root: Path, day: date = TUESDAY
):  # type: ignore[no-untyped-def]
    return fetch_day(
        provider, store, root, CLOCK, instrument_id="fx.eurusd", day=day, point_size=POINT
    )


def test_a_day_with_ticks_is_complete_and_its_file_matches_its_row(tmp_path: Path) -> None:
    store = InMemoryTickDayStore()
    row = _fetch(FakeTickProvider(day_ticks), store, tmp_path)
    assert row.outcome is TickDayOutcome.COMPLETE
    assert row.tick_count == 48
    stored = read_day_file(day_path(tmp_path, "fx.eurusd", TUESDAY))
    assert row.file_sha256 == day_digest("fx.eurusd", TUESDAY, POINT, stored)
    assert row.first_time_ms == int(stored.time_ms[0])
    assert row.last_time_ms == int(stored.time_ms[-1])
    assert store.recorded == [row]


def test_a_day_is_fetched_as_24_contiguous_hours(tmp_path: Path) -> None:
    provider = FakeTickProvider(day_ticks)
    _fetch(provider, InMemoryTickDayStore(), tmp_path)
    starts = [start for _instrument, start, _end in provider.calls]
    ends = [end for _instrument, _start, end in provider.calls]
    midnight = datetime(2026, 10, 6, tzinfo=UTC)
    assert starts == [midnight + timedelta(hours=h) for h in range(24)]
    assert ends == [midnight + timedelta(hours=h + 1) for h in range(24)]


def test_a_day_without_ticks_is_empty_and_writes_no_file(tmp_path: Path) -> None:
    row = _fetch(FakeTickProvider(), InMemoryTickDayStore(), tmp_path)
    assert row.outcome is TickDayOutcome.EMPTY
    assert not day_path(tmp_path, "fx.eurusd", TUESDAY).exists()


def test_bad_data_is_a_failed_day_with_our_own_reason(tmp_path: Path) -> None:
    printed = FakeTickProvider(lambda day: raw_ticks([1_791_288_000_000], last=1.1))
    row = _fetch(printed, InMemoryTickDayStore(), tmp_path)
    assert row.outcome is TickDayOutcome.FAILED
    assert row.detail is not None
    assert "trade print" in row.detail
    assert not day_path(tmp_path, "fx.eurusd", TUESDAY).exists()


def test_a_lost_broker_is_recorded_then_raised(tmp_path: Path) -> None:
    store = InMemoryTickDayStore()
    with pytest.raises(BrokerUnavailableError):
        _fetch(FakeTickProvider(failing=frozenset({TUESDAY})), store, tmp_path)
    assert [row.outcome for row in store.recorded] == [TickDayOutcome.FAILED]
    assert store.recorded[0].detail == "BrokerUnavailableError"


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 7, 0, 0, 30, tzinfo=UTC), date(2026, 10, 5)),
        (datetime(2026, 10, 7, 0, 1, 0, tzinfo=UTC), date(2026, 10, 6)),
        (datetime(2026, 10, 7, 23, 0, tzinfo=UTC), date(2026, 10, 6)),
    ],
)
def test_a_day_is_closed_one_minute_after_midnight(now: datetime, expected: date) -> None:
    assert last_closed_day(FixedClock(now)) == expected


def test_backfill_walks_back_to_until_and_skips_settled_days(tmp_path: Path) -> None:
    store = InMemoryTickDayStore()
    provider = FakeTickProvider(weekdays_only)
    _fetch(provider, store, tmp_path, day=date(2026, 10, 5))
    provider.calls.clear()
    summary = backfill_ticks(
        provider,
        store,
        tmp_path,
        CLOCK,
        instrument_id="fx.eurusd",
        point_size=POINT,
        until=date(2026, 10, 1),
    )
    # Tue 6 back to Thu 1, minus Mon 5 which is settled.
    assert provider.days_called() == [
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 3),
        date(2026, 10, 4),
        date(2026, 10, 6),
    ]
    assert summary.wall_reached is False
    assert summary.earliest_complete == date(2026, 10, 1)


def test_backfill_retries_a_failed_day(tmp_path: Path) -> None:
    store = InMemoryTickDayStore()
    with pytest.raises(BrokerUnavailableError):
        _fetch(FakeTickProvider(failing=frozenset({TUESDAY})), store, tmp_path)
    backfill_ticks(
        FakeTickProvider(weekdays_only),
        store,
        tmp_path,
        CLOCK,
        instrument_id="fx.eurusd",
        point_size=POINT,
        until=TUESDAY,
    )
    assert [row.outcome for row in store.rows("fx.eurusd")] == [
        TickDayOutcome.FAILED,
        TickDayOutcome.COMPLETE,
    ]


def test_backfill_stops_at_five_empty_weekdays(tmp_path: Path) -> None:
    """Ticks only from Thu 1 Oct onward. Walking back from Tue 6: Sep 30 .. Sep 24 are
    five weekdays (30, 29, 28, 25, 24) plus a weekend; the walk stops after the fifth."""

    first_tick_day = date(2026, 10, 1)
    provider = FakeTickProvider(
        lambda day: weekdays_only(day) if day >= first_tick_day else RawTicks.empty()
    )
    summary = backfill_ticks(
        provider,
        InMemoryTickDayStore(),
        tmp_path,
        CLOCK,
        instrument_id="fx.eurusd",
        point_size=POINT,
        until=date(2026, 1, 1),
    )
    assert summary.wall_reached is True
    assert summary.earliest_complete == first_tick_day
    assert min(provider.days_called()) == date(2026, 9, 24)


def test_update_fills_from_the_latest_settled_day_to_the_last_closed_day(tmp_path: Path) -> None:
    store = InMemoryTickDayStore()
    provider = FakeTickProvider(weekdays_only)
    _fetch(provider, store, tmp_path, day=date(2026, 10, 2))
    provider.calls.clear()
    update_ticks(provider, store, tmp_path, CLOCK, instrument_id="fx.eurusd", point_size=POINT)
    assert provider.days_called() == [
        date(2026, 10, 3),
        date(2026, 10, 4),
        date(2026, 10, 5),
        date(2026, 10, 6),
    ]


def test_update_refuses_before_any_backfill(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        update_ticks(
            FakeTickProvider(),
            InMemoryTickDayStore(),
            tmp_path,
            CLOCK,
            instrument_id="fx.eurusd",
            point_size=POINT,
        )
