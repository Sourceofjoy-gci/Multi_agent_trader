"""Hypothesis properties for the market-data invariants (I-17 and paging).

Follows the style of ``tests/property/test_magic.py``: plain ``@given``,
no custom settings profile, strategies module-scoped so several properties
can share them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from trading_house.marketdata.models import BarQuality, Timeframe, availability_of, duration
from trading_house.marketdata.paging import PAGE_BARS, plan_backward
from trading_house.marketdata.quality import assess

TIMEFRAMES = st.sampled_from(list(Timeframe))

INSTANTS = st.datetimes(
    min_value=datetime(1971, 1, 1),
    max_value=datetime(9999, 12, 30),
).map(lambda naive: naive.replace(tzinfo=UTC))

# High enough that subtracting even the largest generated span (a D1 page,
# ~192 years at the top multiplier below) can never underflow datetime.MINYEAR.
PAGING_NEWEST_INSTANTS = st.datetimes(
    min_value=datetime(2200, 1, 1),
    max_value=datetime(9999, 12, 30),
).map(lambda naive: naive.replace(tzinfo=UTC))

SERVER_OFFSETS = st.integers(min_value=-14 * 3600, max_value=14 * 3600)

# A page spans duration(timeframe) * PAGE_BARS -- 833 days for H1, 3,333 for
# H4, 20,000 for D1. A span capped at a fixed day count only ever produces a
# single page for those three timeframes, so the tiling loop that checks
# adjacent pages never runs for them. Scaling the span to a multiple of the
# timeframe's own page width instead guarantees both a single-page and a
# multi-page range for every timeframe, including 0 to also keep the empty
# and near-empty-range paths exercised.
PAGE_SPAN_MULTIPLIERS = st.floats(
    min_value=0.0, max_value=3.5, allow_nan=False, allow_infinity=False
)

ALIGNED_OFFSETS = st.sampled_from([0, 1800, 7200, 10800, -18000])
ALIGNMENT_PERIODS = st.integers(min_value=0, max_value=500_000)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _aligned_event_time(timeframe: Timeframe, offset_seconds: int, periods: int) -> datetime:
    """An ``event_time`` that clears ``is_aligned`` by construction.

    ``is_aligned`` checks that ``event_time.timestamp() + offset_seconds`` is
    an exact multiple of the timeframe's seconds. Rather than generating an
    arbitrary instant and hoping it happens to land on that boundary --
    vanishingly unlikely for anything above M1, and never for D1's 86,400
    second step -- this inverts that arithmetic directly, so alignment holds
    by construction for every generated example.

    Built from the epoch with ``timedelta`` rather than
    ``datetime.fromtimestamp``: the latter calls into the platform C library
    and rejects pre-1970 instants on Windows, where these tests also run;
    epoch-plus-timedelta is pure Python arithmetic and has no such limit.
    """

    step = int(duration(timeframe).total_seconds())
    return _EPOCH + timedelta(seconds=periods * step - offset_seconds)


OHLC_VALUES = st.decimals(
    min_value=Decimal("-1000"),
    max_value=Decimal("1000000"),
    places=5,
    allow_nan=False,
)

POSITIVE_PRICES = st.decimals(
    min_value=Decimal("0.00001"),
    max_value=Decimal("1000000"),
    places=5,
    allow_nan=False,
)

SPREADS = st.integers(min_value=-10_000, max_value=10_000)
NON_NEGATIVE_SPREADS = st.integers(min_value=0, max_value=10_000)


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


@given(TIMEFRAMES, PAGING_NEWEST_INSTANTS, PAGE_SPAN_MULTIPLIERS)
def test_pages_always_tile_their_range(
    timeframe: Timeframe, newest: datetime, multiplier: float
) -> None:
    """No overlap, no hole, for any timeframe and any ``(newest, oldest)``
    pair whose span runs from zero up to several multiples of that
    timeframe's own page width -- enough to force both a single-page and a
    multi-page range for every timeframe, not just the finest ones. A hole
    here is a permanently missing window nobody notices."""

    page_span = duration(timeframe) * PAGE_BARS
    oldest = newest - page_span * multiplier
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
    ALIGNED_OFFSETS,
    ALIGNMENT_PERIODS,
)
def test_a_bar_passing_the_gates_always_satisfies_ohlc_ordering(
    bar_open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    spread: int,
    timeframe: Timeframe,
    offset_seconds: int,
    periods: int,
) -> None:
    """Any bar that ``quality.assess`` grades ``OK`` satisfies OHLC ordering
    and carries only positive prices -- whatever arbitrary values it was
    handed.

    ``event_time`` is built to be aligned by construction (see
    ``_aligned_event_time``): an arbitrary instant almost never clears
    ``is_aligned``, which would leave every example falling through to
    ``MISALIGNED_TIMESTAMP`` and this test's own pass branch permanently
    unreached -- exactly the failure mode
    ``test_ohlc_ok_is_actually_reachable`` below exists to catch.
    """

    event_time = _aligned_event_time(timeframe, offset_seconds, periods)
    quality = assess(
        bar_open=bar_open,
        high=high,
        low=low,
        close=close,
        spread=spread,
        timeframe=timeframe,
        event_time=event_time,
        server_zone=timezone(timedelta(seconds=offset_seconds)),
    )

    if quality is not BarQuality.OK:
        return

    assert min(bar_open, high, low, close) > 0
    assert low <= min(bar_open, close) <= max(bar_open, close) <= high


@given(POSITIVE_PRICES, NON_NEGATIVE_SPREADS, TIMEFRAMES, ALIGNED_OFFSETS, ALIGNMENT_PERIODS)
def test_ohlc_ok_is_actually_reachable(
    price: Decimal, spread: int, timeframe: Timeframe, offset_seconds: int, periods: int
) -> None:
    """The property above has an empty pass branch unless ``assess()`` can
    actually return ``OK`` for *some* input -- a property test whose pass
    branch never runs is indistinguishable from one that always returns
    early, and passes just as green either way. A flat bar (all four prices
    equal, so OHLC coherence is trivial), a non-negative spread, and an
    aligned event time clear every gate by construction, so this must always
    grade ``OK``; if it ever stops, the search above would have gone quietly
    vacuous again.
    """

    event_time = _aligned_event_time(timeframe, offset_seconds, periods)
    quality = assess(
        bar_open=price,
        high=price,
        low=price,
        close=price,
        spread=spread,
        timeframe=timeframe,
        event_time=event_time,
        server_zone=timezone(timedelta(seconds=offset_seconds)),
    )

    assert quality is BarQuality.OK
