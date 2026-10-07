"""The four checks a bar must survive to be called clean.

Every one rejects an *impossibility* -- something that cannot be true of any
real bar. None rejects an implausibility. A 500-pip minute is either a real
flash crash or a broker error, and nothing available at write time tells them
apart; a tunable threshold here would mean stored data changed when the
threshold was tuned, and two backtests run either side of that change would
differ with no code change between them (D-7).

Measured against 100,000 real FBS bars, none of these fired. That is the
point: they are the assertion that catches the day something upstream breaks,
including this project's own timestamp conversion.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from decimal import Decimal

from trading_house.marketdata.models import BarQuality, Timeframe, is_aligned


def assess(
    *,
    bar_open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    spread: int,
    timeframe: Timeframe,
    event_time: datetime,
    server_zone: tzinfo,
) -> BarQuality:
    """Classify one bar. The first defect found wins, in a fixed order."""

    if min(bar_open, high, low, close) <= 0:
        return BarQuality.NON_POSITIVE_PRICE
    if not (low <= min(bar_open, close) and max(bar_open, close) <= high):
        return BarQuality.OHLC_INCOHERENT
    if spread < 0:
        return BarQuality.NEGATIVE_SPREAD
    if not is_aligned(timeframe, event_time, server_zone=server_zone):
        return BarQuality.MISALIGNED_TIMESTAMP
    return BarQuality.OK
