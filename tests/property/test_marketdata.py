"""Hypothesis properties for the market-data invariants (I-17 and paging).

Follows the style of ``tests/property/test_magic.py``: plain ``@given``,
no custom settings profile, strategies module-scoped so several properties
can share them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from trading_house.marketdata.models import BarQuality, Timeframe, availability_of, duration
from trading_house.marketdata.paging import plan_backward
from trading_house.marketdata.quality import assess

TIMEFRAMES = st.sampled_from(list(Timeframe))

INSTANTS = st.datetimes(
    min_value=datetime(1971, 1, 1),
    max_value=datetime(9999, 12, 30),
).map(lambda naive: naive.replace(tzinfo=UTC))

SERVER_OFFSETS = st.integers(min_value=-14 * 3600, max_value=14 * 3600)

RANGE_SPANS = st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=400))

OHLC_VALUES = st.decimals(
    min_value=Decimal("-1000"),
    max_value=Decimal("1000000"),
    places=5,
    allow_nan=False,
)

SPREADS = st.integers(min_value=-10_000, max_value=10_000)


@given(TIMEFRAMES, INSTANTS, SERVER_OFFSETS)
def test_availability_always_follows_the_event(
    timeframe: Timeframe, event_time: datetime, server_offset_seconds: int
) -> None:
    """No timeframe, no instant, no server offset makes a bar knowable before
    it closed.

    ``availability_of`` takes no offset argument -- it operates purely in
    UTC, after the boundary conversion has already happened -- so the
    ordering must hold identically no matter what server offset the caller
    happened to be dealing with. ``server_offset_seconds`` is generated and
    deliberately never passed to ``availability_of``: the property does not
    get to depend on it, which is itself the point being tested.
    """

    del server_offset_seconds

    availability_time = availability_of(timeframe, event_time)

    assert availability_time > event_time
    assert availability_time == event_time + duration(timeframe)


@given(TIMEFRAMES, INSTANTS, RANGE_SPANS)
def test_pages_always_tile_their_range(
    timeframe: Timeframe, newest: datetime, span: timedelta
) -> None:
    """No overlap, no hole, for any timeframe and any ``(newest, oldest)``
    pair. A hole here is a permanently missing window nobody notices."""

    oldest = newest - span
    pages = plan_backward(timeframe, newest=newest, oldest=oldest)

    if not pages:
        assert oldest == newest
        return

    assert pages[0][1] == newest
    assert pages[-1][0] == oldest
    for start, end in pages:
        assert start < end
    for older, newer in zip(pages[1:], pages[:-1], strict=True):
        assert older[1] == newer[0]


@given(
    OHLC_VALUES,
    OHLC_VALUES,
    OHLC_VALUES,
    OHLC_VALUES,
    SPREADS,
    TIMEFRAMES,
    INSTANTS,
    SERVER_OFFSETS,
)
def test_a_bar_passing_the_gates_always_satisfies_ohlc_ordering(
    bar_open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    spread: int,
    timeframe: Timeframe,
    event_time: datetime,
    server_offset_seconds: int,
) -> None:
    """Any bar that ``quality.assess`` grades ``OK`` satisfies OHLC ordering
    and carries only positive prices -- whatever arbitrary values it was
    handed."""

    quality = assess(
        bar_open=bar_open,
        high=high,
        low=low,
        close=close,
        spread=spread,
        timeframe=timeframe,
        event_time=event_time,
        server_offset_seconds=server_offset_seconds,
    )

    if quality is not BarQuality.OK:
        return

    assert min(bar_open, high, low, close) > 0
    assert low <= min(bar_open, close) <= max(bar_open, close) <= high
