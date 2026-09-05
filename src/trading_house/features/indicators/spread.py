"""Typical spread over a window, in points. Pure.

Points, not price: converting needs the instrument contract's price
increment, which lives behind the broker adapter, and reaching for it here
would make ``features/`` depend on ``brokers/``. Section 8.2's caller already
holds the contract it needs to convert.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from decimal import Decimal

from trading_house.core.errors import InsufficientHistoryError
from trading_house.marketdata.models import Bar


def median_spread_points(bars: Sequence[Bar]) -> Decimal:
    """The median bar spread across ``bars``.

    Median rather than mean or latest: a single wide print is common and
    would otherwise widen every stop sized against it.
    """

    if not bars:
        raise InsufficientHistoryError
    return statistics.median(Decimal(bar.spread) for bar in bars)
