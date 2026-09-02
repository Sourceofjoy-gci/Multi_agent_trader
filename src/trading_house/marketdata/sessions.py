"""When FX and metals are liquid, and how many bars a window should hold.

This exists so ``coverage_ratio`` means something. Without a session model a
seven-day window expects seven days of bars, every honest run comes back at
five sevenths, and the one signal this phase needs -- a run that returned
almost nothing -- drowns in false alarms.

The boundaries are measured, not assumed: FBS closes near 21:00 UTC on Friday
and reopens near 21:00 UTC on Sunday. Equities are out of scope (D-4) and
would need a real exchange calendar rather than this.
"""

from __future__ import annotations

from datetime import datetime

from trading_house.marketdata.models import Timeframe, duration

WEEK_CLOSE_WEEKDAY = 4  # Friday
WEEK_OPEN_WEEKDAY = 6  # Sunday
WEEK_BOUNDARY_UTC_HOUR = 21

_MAX_STEPS = 200_000


def is_liquid(instant: datetime) -> bool:
    """Whether FX and metals trade at this instant."""

    weekday = instant.weekday()
    if weekday == 5:  # Saturday
        return False
    if weekday == WEEK_CLOSE_WEEKDAY:
        return instant.hour < WEEK_BOUNDARY_UTC_HOUR
    if weekday == WEEK_OPEN_WEEKDAY:
        return instant.hour >= WEEK_BOUNDARY_UTC_HOUR
    return True


def expected_bars(timeframe: Timeframe, start: datetime, end: datetime) -> int:
    """How many bars a liquid market would produce across ``[start, end)``.

    Walks the range at the timeframe's own step and counts the liquid ones.
    Exact rather than clever: the ranges here are bounded by a page, and an
    off-by-one in a closed-form weekend calculation would quietly bias every
    coverage ratio in the system.
    """

    if end <= start:
        return 0
    step = duration(timeframe)
    if (end - start) / step > _MAX_STEPS:
        raise ValueError("range too large to count bar by bar")
    count = 0
    cursor = start
    while cursor < end:
        if is_liquid(cursor):
            count += 1
        cursor += step
    return count
