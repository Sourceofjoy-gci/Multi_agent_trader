"""True range and Wilder's ATR. Pure: bars in, a Decimal out.

These feed stop distance in the master specification's section 8.2, which
feeds lot size in 8.1. A wrong number here is a wrong stop and a wrong
position on a live order, so the correctness bar is the execution path's.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise

from trading_house.core.errors import InsufficientHistoryError
from trading_house.marketdata.models import Bar


def true_range(high: Decimal, low: Decimal, previous_close: Decimal) -> Decimal:
    """The widest of the bar's own range and its two gaps from the last close.

    The gap terms are why this is not simply high minus low: a bar that opens
    away from the previous close covered that distance too, and a stop sized
    on the bar alone would ignore it.
    """

    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def wilder_atr(bars: Sequence[Bar], period: int) -> Decimal:
    """Wilder's average true range over ``bars``.

    Recursive by definition: each value is smoothed from the one before, and
    the chain terminates in a seed averaged over the first ``period`` true
    ranges. That makes the result depend on where the series starts, which is
    why the engine always hands this a fixed-size window rather than whatever
    the store happens to hold.
    """

    if period <= 0:
        raise ValueError("period must be positive")
    if len(bars) < period + 1:
        raise InsufficientHistoryError

    ranges = [
        true_range(current.high, current.low, previous.close)
        for previous, current in pairwise(bars)
    ]
    atr = sum(ranges[:period], start=Decimal(0)) / period
    for value in ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr
