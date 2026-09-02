"""Split a history request into windows the broker will actually answer.

MetaTrader 5 caps a request by the size of its result, not by how old the data
is: 50,000 bars comes back, 100,000 fails with ``Invalid params``, and a range
query spanning 2000 to now fails for M1 through H1 for the same reason while
succeeding for D1. So pages are sized in bars and converted to a window per
timeframe.

Pages run newest to oldest because backfill walks backwards from the oldest
bar already stored.
"""

from __future__ import annotations

from datetime import datetime

from trading_house.marketdata.models import Timeframe, duration

PAGE_BARS = 20_000
"""Well under the measured 50,000-row ceiling, leaving room for a broker whose
cap is lower than FBS's."""


def plan_backward(
    timeframe: Timeframe, *, newest: datetime, oldest: datetime
) -> tuple[tuple[datetime, datetime], ...]:
    """Tile ``[oldest, newest)`` into windows, newest first."""

    if oldest > newest:
        raise ValueError("oldest must not be after newest")
    span = duration(timeframe) * PAGE_BARS
    pages: list[tuple[datetime, datetime]] = []
    end = newest
    while end > oldest:
        start = max(oldest, end - span)
        pages.append((start, end))
        end = start
    return tuple(pages)
