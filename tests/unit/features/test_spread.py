from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.indicators.spread import median_spread_points
from trading_house.marketdata.models import Bar, BarQuality, Timeframe

BASE = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _bar(minute: int, spread: int) -> Bar:
    opened = BASE + timedelta(minutes=minute)
    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        event_time=opened,
        availability_time=opened + timedelta(minutes=1),
        open=Decimal("1.1"),
        high=Decimal("1.2"),
        low=Decimal("1.0"),
        close=Decimal("1.15"),
        tick_volume=10,
        spread=spread,
        real_volume=0,
        quality=BarQuality.OK,
    )


def test_an_odd_window_takes_the_middle_value() -> None:
    assert median_spread_points([_bar(0, 5), _bar(1, 9), _bar(2, 7)]) == Decimal("7")


def test_an_even_window_averages_the_two_middle_values() -> None:
    """Which is why this returns Decimal rather than the int the store holds."""

    assert median_spread_points([_bar(0, 5), _bar(1, 8), _bar(2, 6), _bar(3, 9)]) == Decimal("7")


def test_one_spike_does_not_move_the_median() -> None:
    """The reason for median over mean or latest: a single wide print would
    otherwise widen every stop derived from it."""

    calm = [_bar(i, 6) for i in range(9)]
    spiked = [*calm, _bar(9, 900)]

    assert median_spread_points(spiked) == Decimal("6")


def test_an_empty_window_is_a_refusal_not_a_zero() -> None:
    """A zero spread would read as a free market and shrink the cost term of
    every stop computed from it."""

    with pytest.raises(InsufficientHistoryError):
        median_spread_points([])
