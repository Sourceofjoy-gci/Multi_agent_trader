from datetime import UTC, datetime, timedelta

import pytest

from trading_house.marketdata.models import Timeframe
from trading_house.marketdata.sessions import expected_bars, is_liquid


def test_midweek_is_liquid() -> None:
    assert is_liquid(datetime(2026, 8, 26, 12, 0, tzinfo=UTC))  # a Wednesday


def test_saturday_is_not_liquid() -> None:
    assert not is_liquid(datetime(2026, 8, 29, 12, 0, tzinfo=UTC))


def test_the_week_closes_friday_evening_and_reopens_sunday_evening() -> None:
    """Measured on FBS: ~48h gaps ending Friday ~21:00 UTC."""

    assert is_liquid(datetime(2026, 8, 28, 20, 0, tzinfo=UTC))  # Friday 20:00
    assert not is_liquid(datetime(2026, 8, 28, 22, 0, tzinfo=UTC))  # Friday 22:00
    assert not is_liquid(datetime(2026, 8, 30, 20, 0, tzinfo=UTC))  # Sunday 20:00
    assert is_liquid(datetime(2026, 8, 30, 22, 0, tzinfo=UTC))  # Sunday 22:00


def test_a_full_liquid_day_expects_one_bar_per_minute() -> None:
    start = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)

    assert expected_bars(Timeframe.M1, start, start + timedelta(days=1)) == 1440


def test_a_weekend_expects_nothing() -> None:
    start = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)  # Saturday

    assert expected_bars(Timeframe.M1, start, start + timedelta(days=1)) == 0


def test_a_full_week_excludes_the_weekend() -> None:
    """The property that makes coverage_ratio meaningful: a seven-day window
    must not expect seven days of bars, or every honest run reads as sparse."""

    start = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)  # Monday
    week = expected_bars(Timeframe.M1, start, start + timedelta(days=7))

    assert 5 * 1440 <= week <= 6 * 1440


def test_higher_timeframes_scale_down() -> None:
    start = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
    day = timedelta(days=1)

    assert expected_bars(Timeframe.H1, start, start + day) == 24
    assert expected_bars(Timeframe.H4, start, start + day) == 6


def test_an_inverted_range_expects_nothing_rather_than_a_negative_count() -> None:
    later = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    assert expected_bars(Timeframe.M1, later, later - timedelta(days=1)) == 0


def test_a_range_far_larger_than_any_page_is_refused_rather_than_walked() -> None:
    """Ten years of M1 is five million steps. Pages are bounded by design
    (Task 4); a caller asking for more has a bug, and spinning is a worse
    answer than saying so."""

    start = datetime(2016, 1, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="range too large"):
        expected_bars(Timeframe.M1, start, datetime(2026, 1, 1, tzinfo=UTC))


def test_the_week_boundary_itself_is_tested_not_just_either_side() -> None:
    """21:00 is the boundary. Testing 20:00 and 22:00 leaves a whole hour in
    which `<` could become `<=` and every test would still pass -- while every
    coverage ratio in the system shifted."""

    assert not is_liquid(datetime(2026, 8, 28, 21, 0, tzinfo=UTC))  # Friday close
    assert is_liquid(datetime(2026, 8, 28, 20, 59, tzinfo=UTC))  # last liquid minute
    assert is_liquid(datetime(2026, 8, 30, 21, 0, tzinfo=UTC))  # Sunday reopen
    assert not is_liquid(datetime(2026, 8, 30, 20, 59, tzinfo=UTC))  # still closed
