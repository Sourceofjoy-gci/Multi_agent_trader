"""Hypothesis properties for the pure indicators (I-18's foundation).

Follows the style of ``tests/property/test_marketdata.py``: plain ``@given``,
strategies module-scoped. The ATR property builds its bars *by construction*
rather than by filtering an unconstrained strategy with ``assume`` -- ``Bar``
rejects an incoherent OHLC bar, and Phase 1.5 already shipped a property test
whose pass branch was reached in 0 of 2,000 examples because its strategy
generated almost nothing valid. Constructing a low, a non-negative span, and
deriving high/open/close from them makes every generated bar valid, so the
budget is spent on the assertion instead of on rejections.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from hypothesis import given
from hypothesis import strategies as st

from trading_house.features.indicators.volatility import true_range, wilder_atr
from trading_house.marketdata.models import Bar, BarQuality, Timeframe

BASE = datetime(2026, 1, 1, tzinfo=UTC)

PRICES = st.decimals(min_value=Decimal("0.1"), max_value=Decimal("1000"), places=5)


@given(high=PRICES, low=PRICES, previous_close=PRICES)
def test_true_range_is_never_negative(high: Decimal, low: Decimal, previous_close: Decimal) -> None:
    """It is a distance. A negative one would shrink a stop toward zero."""

    assert true_range(high, low, previous_close) >= 0


@given(high=PRICES, low=PRICES, previous_close=PRICES)
def test_true_range_is_at_least_the_bar_s_own_range(
    high: Decimal, low: Decimal, previous_close: Decimal
) -> None:
    """The gap terms can only widen it, never narrow it."""

    assert true_range(high, low, previous_close) >= high - low


@st.composite
def _coherent_bars(draw: st.DrawFn, count: int) -> list[Bar]:
    """``count`` bars, each valid by construction: ``low`` and a non-negative
    ``span`` fix ``high``, and the midpoint stands in for open and close, so
    ``low <= open, close <= high`` holds for every draw rather than for the
    ones that happen to survive a filter."""

    bars = []
    for index in range(count):
        low = draw(PRICES)
        span = draw(st.decimals(min_value=Decimal("0"), max_value=Decimal("50"), places=5))
        high = low + span
        mid = low + span / 2
        opened = BASE + timedelta(minutes=index)
        bars.append(
            Bar(
                instrument_id="fx.eurusd",
                timeframe=Timeframe.M1,
                event_time=opened,
                availability_time=opened + timedelta(minutes=1),
                open=mid,
                high=high,
                low=low,
                close=mid,
                tick_volume=1,
                spread=1,
                real_volume=0,
                quality=BarQuality.OK,
            )
        )
    return bars


@given(bars=_coherent_bars(count=20))
def test_atr_never_exceeds_the_widest_true_range_it_saw(bars: list[Bar]) -> None:
    """An average cannot exceed its own maximum. If it does, the smoothing
    recursion is wrong in a way that would widen every stop built on it."""

    widest = max(
        true_range(current.high, current.low, previous.close)
        for previous, current in pairwise(bars)
    )
    result = wilder_atr(bars, period=5)

    assert 0 <= result <= widest


@st.composite
def _constant_true_range_bars(draw: st.DrawFn, count: int) -> tuple[list[Bar], Decimal]:
    """``count`` bars sharing one fixed ``low``/``high``/``close``, so every
    true range in the series -- including the first, since the previous
    close equals every bar's own low -- is exactly the drawn ``span``.

    A constant input series is Wilder's smoothing's fixed point: the seed is
    the average of ``period`` copies of ``span``, i.e. ``span`` itself, and
    ``(span * (period - 1) + span) / period`` is ``span`` again, so the
    result must reproduce ``span`` exactly, not merely fall inside a range.
    """

    low = draw(PRICES)
    span = draw(st.decimals(min_value=Decimal("0"), max_value=Decimal("50"), places=5))
    high = low + span
    bars = []
    for index in range(count):
        opened = BASE + timedelta(minutes=index)
        bars.append(
            Bar(
                instrument_id="fx.eurusd",
                timeframe=Timeframe.M1,
                event_time=opened,
                availability_time=opened + timedelta(minutes=1),
                open=low,
                high=high,
                low=low,
                close=low,
                tick_volume=1,
                spread=1,
                real_volume=0,
                quality=BarQuality.OK,
            )
        )
    return bars, span


@given(drawn=_constant_true_range_bars(count=20))
def test_atr_reproduces_a_constant_true_range_exactly(drawn: tuple[list[Bar], Decimal]) -> None:
    """The bound in the property above is satisfied by ``wilder_atr`` always
    returning zero, by picking an arbitrary true range out of the window, or
    by any wrong smoothing weight that happens to land inside the range --
    none of those survive this one, because a constant series has only one
    correct answer, not a range of acceptable ones."""

    bars, span = drawn

    assert wilder_atr(bars, period=5) == span
