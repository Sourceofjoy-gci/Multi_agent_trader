"""Features over a fixed window, so the same instant always gives the same
number.

Only this class holds the bar store. A caller cannot obtain a feature without
going through a point-in-time read, which keeps the guarantee Phase 1.5 built
structural rather than conventional.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.indicators.spread import median_spread_points as _median_spread
from trading_house.features.indicators.volatility import wilder_atr
from trading_house.marketdata.models import Bar, Coverage, Timeframe, duration

WARMUP_MULTIPLE = 10
"""How many periods of history an indicator is given.

Wilder's smoothing is recursive, so its value depends on where the series
started. Ten periods puts the seed's influence below anything that matters
while keeping the window a constant rather than a judgement made at each
call site.
"""

SPAN_SAFETY = 3
"""How much wider than the bar count to make the time range.

N bars do not span N periods of calendar time -- a week of M1 is about 7,200
bars, not 10,080, because the weekend is closed. Asking wide and slicing back
is what makes the answer independent of gaps; three covers weekends with room
for holidays.
"""


@runtime_checkable
class BarReader(Protocol):
    """The read surface a feature needs. Deliberately narrower than BarStore,
    which also carries the write path features must never reach."""

    def bars(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
        as_of: datetime,
        include_defective: bool = False,
    ) -> tuple[Bar, ...]: ...

    def coverage(self, instrument_id: str, timeframe: Timeframe) -> Coverage: ...


class FeatureEngine:
    """Computes features from stored bars over a deterministic window."""

    def __init__(self, store: BarReader) -> None:
        self._store = store

    def atr(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        period: int,
        as_of: datetime,
    ) -> Decimal:
        """Wilder's ATR over ``period * WARMUP_MULTIPLE`` bars ending at ``as_of``."""

        bars = self._window(
            instrument_id, timeframe, count=period * WARMUP_MULTIPLE + 1, as_of=as_of
        )
        return wilder_atr(bars, period)

    def median_spread_points(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        window: int,
        as_of: datetime,
    ) -> Decimal:
        """The median bar spread over exactly ``window`` bars ending at ``as_of``."""

        bars = self._window(instrument_id, timeframe, count=window, as_of=as_of)
        return _median_spread(bars)

    def _window(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        count: int,
        as_of: datetime,
    ) -> Sequence[Bar]:
        """The most recent ``count`` bars knowable at ``as_of``, or a refusal.

        The store reads by range while the window is a count, so this asks
        wide and slices. Two clamps on the range matter, both because a
        naive ``as_of``-relative window can miss real data entirely:

        - The low end is clamped to ``coverage.earliest_event_time``:
          ``bars()`` raises ``CoverageError`` when the requested start
          precedes the earliest stored bar, which a generous range would
          trigger on a young store that nonetheless holds enough bars.
        - The span backward is anchored on ``min(as_of, latest_event_time)``,
          not on ``as_of`` alone: ``as_of`` is a point-in-time ceiling, not a
          promise that data exists nearby it. Anchoring on ``as_of`` when the
          store's last bar is much older would compute a ``start`` that
          overshoots every real bar and return nothing, even though the
          store holds plenty of history available as of ``as_of``.
        """

        coverage = self._store.coverage(instrument_id, timeframe)
        if coverage.earliest_event_time is None or coverage.latest_event_time is None:
            raise InsufficientHistoryError

        span = duration(timeframe) * count * SPAN_SAFETY
        anchor = min(as_of, coverage.latest_event_time)
        start = max(anchor - span, coverage.earliest_event_time)
        bars = self._store.bars(instrument_id, timeframe, start=start, end=as_of, as_of=as_of)
        if len(bars) < count:
            raise InsufficientHistoryError
        return bars[-count:]
