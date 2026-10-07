from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.marketdata.tick_store import TickDay, TickDayOutcome, coverage_of, settled_days

FETCHED = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)
POINT = Decimal("0.00001")
SHA = "a" * 64


def _complete(day: date, ticks: int = 10) -> TickDay:
    return TickDay(
        instrument_id="fx.eurusd",
        day=day,
        outcome=TickDayOutcome.COMPLETE,
        tick_count=ticks,
        first_time_ms=1,
        last_time_ms=2,
        crossed_quotes=0,
        point_size=POINT,
        file_sha256=SHA,
        fetched_at=FETCHED,
    )


def _empty(day: date) -> TickDay:
    return TickDay(
        instrument_id="fx.eurusd",
        day=day,
        outcome=TickDayOutcome.EMPTY,
        tick_count=0,
        point_size=POINT,
        fetched_at=FETCHED,
    )


def _failed(day: date) -> TickDay:
    return TickDay(
        instrument_id="fx.eurusd",
        day=day,
        outcome=TickDayOutcome.FAILED,
        tick_count=0,
        point_size=POINT,
        detail="BrokerUnavailableError",
        fetched_at=FETCHED,
    )


def test_a_complete_day_needs_a_digest() -> None:
    with pytest.raises(ValidationError):
        TickDay(
            instrument_id="fx.eurusd",
            day=date(2026, 10, 6),
            outcome=TickDayOutcome.COMPLETE,
            tick_count=10,
            first_time_ms=1,
            last_time_ms=2,
            point_size=POINT,
            fetched_at=FETCHED,
        )


def test_an_empty_day_has_no_ticks() -> None:
    with pytest.raises(ValidationError):
        TickDay(
            instrument_id="fx.eurusd",
            day=date(2026, 10, 6),
            outcome=TickDayOutcome.EMPTY,
            tick_count=3,
            point_size=POINT,
            fetched_at=FETCHED,
        )


def test_a_failed_day_needs_a_detail() -> None:
    with pytest.raises(ValidationError):
        TickDay(
            instrument_id="fx.eurusd",
            day=date(2026, 10, 6),
            outcome=TickDayOutcome.FAILED,
            tick_count=0,
            point_size=POINT,
            fetched_at=FETCHED,
        )


@pytest.mark.parametrize(
    "shape",
    [
        {"detail": ""},
        {"file_sha256": SHA},
        {"tick_count": 1},
        {"crossed_quotes": 1},
        {"first_time_ms": 1},
        {"last_time_ms": 2},
    ],
    ids=["empty-detail", "digest", "ticks", "crossed", "first-time", "last-time"],
)
def test_a_failed_day_has_a_detail_and_nothing_else(shape: dict[str, object]) -> None:
    """Matches migration 0010's tick_days_failed_shape exactly: a failed fetch
    stored no ticks, so it has no count, no times and no digest."""

    fields = _failed(date(2026, 10, 6)).model_dump() | shape
    with pytest.raises(ValidationError):
        TickDay(**fields)


def test_the_failed_shape_accepts_a_failed_day() -> None:
    fields = _failed(date(2026, 10, 6)).model_dump()
    assert TickDay(**fields).outcome is TickDayOutcome.FAILED


def test_settled_days_ignore_failures() -> None:
    rows = (_failed(date(2026, 10, 5)), _complete(date(2026, 10, 6)), _failed(date(2026, 10, 6)))
    settled = settled_days(rows)
    assert set(settled) == {date(2026, 10, 6)}
    assert settled[date(2026, 10, 6)].outcome is TickDayOutcome.COMPLETE


def test_coverage_counts_and_finds_weekday_gaps() -> None:
    """Mon 5th and Thu 8th complete, Tue 6th a holiday (EMPTY), Wed 7th failed only:
    the 7th is a weekday gap; the holiday is not. Fri 9th and the following Mon
    12th are also complete with nothing recorded for the Sat 10th/Sun 11th
    weekend in between: the weekend must not show up in ``weekday_gaps``
    alongside the real gap (mutation proof: dropping the weekday check from
    ``coverage_of`` would add the 10th and 11th here)."""

    rows = (
        _complete(date(2026, 10, 5), ticks=7),
        _empty(date(2026, 10, 6)),
        _failed(date(2026, 10, 7)),
        _complete(date(2026, 10, 8), ticks=5),
        _complete(date(2026, 10, 9)),
        _complete(date(2026, 10, 12)),
    )
    coverage = coverage_of("fx.eurusd", rows)
    assert coverage.complete_days == 4
    assert coverage.empty_days == 1
    assert coverage.unsettled_failed_days == 1
    assert coverage.ticks == 32
    assert coverage.earliest_complete == date(2026, 10, 5)
    assert coverage.latest_complete == date(2026, 10, 12)
    assert coverage.weekday_gaps == (date(2026, 10, 7),)
