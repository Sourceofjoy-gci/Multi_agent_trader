from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.indicators.volatility import true_range, wilder_atr
from trading_house.marketdata.models import Bar, BarQuality, Timeframe

BASE = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _bar(minute: int, high: str, low: str, close: str) -> Bar:
    opened = BASE + timedelta(minutes=minute)
    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        event_time=opened,
        availability_time=opened + timedelta(minutes=1),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        tick_volume=10,
        spread=9,
        real_volume=0,
        quality=BarQuality.OK,
    )


# Worked by hand from Wilder's definition, so the expected values below are
# verifiable without running the implementation:
#
#   bar  high  low  close    true range
#   b0    10    10    10     -- (no previous close)
#   b1    12     8    11     max(12-8, |12-10|, |8-10|)  = 4
#   b2    13    11    12     max(13-11, |13-11|, |11-11|) = 2
#   b3    14    10    13     max(14-10, |14-12|, |10-12|) = 4
#   b4    15    13    14     max(15-13, |15-13|, |13-13|) = 2
#
#   period = 2
#   seed  = mean(TR1, TR2)        = (4 + 2) / 2   = 3
#   ATR3  = (3   * 1 + 4) / 2                     = 3.5
#   ATR4  = (3.5 * 1 + 2) / 2                     = 2.75
WORKED = [
    _bar(0, "10", "10", "10"),
    _bar(1, "12", "8", "11"),
    _bar(2, "13", "11", "12"),
    _bar(3, "14", "10", "13"),
    _bar(4, "15", "13", "14"),
]


def test_true_range_takes_the_widest_of_its_three_candidates() -> None:
    assert true_range(Decimal("12"), Decimal("8"), Decimal("10")) == Decimal("4")


def test_true_range_uses_the_gap_when_it_exceeds_the_bar() -> None:
    """A bar that opens far from the previous close has a true range wider
    than its own high-low, which is the whole reason the measure exists."""

    assert true_range(Decimal("21"), Decimal("20"), Decimal("10")) == Decimal("11")


def test_true_range_uses_the_downward_gap_too() -> None:
    """A bar gapping far BELOW the previous close: only the |low - close|
    term can see that distance. Every other test in this file has an upward
    gap, where this term either loses or ties."""

    assert true_range(Decimal("8"), Decimal("5"), Decimal("20")) == Decimal("15")


def test_wilder_atr_matches_the_hand_worked_example() -> None:
    """Expected value derived from the definition above, not from this code."""

    assert wilder_atr(WORKED, period=2) == Decimal("2.75")


def test_wilder_atr_needs_one_more_bar_than_its_period() -> None:
    """The first true range needs a previous close, so a period of 2 needs
    three bars: two true ranges to seed from, and the bar before them."""

    with pytest.raises(InsufficientHistoryError):
        wilder_atr(WORKED[:2], period=2)

    assert wilder_atr(WORKED[:3], period=2) == Decimal("3")


def test_wilder_atr_rejects_a_non_positive_period() -> None:
    with pytest.raises(ValueError, match="period"):
        wilder_atr(WORKED, period=0)

    with pytest.raises(ValueError, match="period"):
        wilder_atr(WORKED, period=-1)


def test_a_flat_series_has_zero_range() -> None:
    flat = [_bar(i, "10", "10", "10") for i in range(5)]

    assert wilder_atr(flat, period=2) == Decimal("0")
