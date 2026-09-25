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
from typing import Protocol

from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.indicators.spread import median_spread_points as _median_spread
from trading_house.features.indicators.volatility import wilder_atr
from trading_house.features.sessions import (
    Session,
    preceding_session_window,
    session_bounds,
    session_of,
)
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

    def prior_session_return(
        self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime
    ) -> Decimal | None:
        """Close-to-close return of the window that ended at or before ``as_of``.

        ``None`` when that window is empty or missing any expected bar --
        a holiday, a data gap, the first window in the store, or a store
        whose coverage only starts partway through the window -- which is
        distinct from a return of zero: a strategy must be able to tell a
        feed outage from a flat night, or it stands down for the wrong
        reason. A partially covered window is not one this can answer about:
        computing a return from whichever bars happen to remain would
        misreport a shorter move as the whole session's.
        """

        _session, start, end = preceding_session_window(as_of)
        coverage = self._store.coverage(instrument_id, timeframe)
        if coverage.earliest_event_time is None or start < coverage.earliest_event_time:
            return None
        # start is the window's own lower bound, so the store is asked for no
        # bar the `event_time < end` filter below would discard anyway.
        bars = [
            bar
            for bar in self._read(instrument_id, timeframe, start=start, as_of=as_of)
            if bar.event_time < end
        ]
        if not bars:
            return None
        if not self._window_is_complete(bars, start, end - duration(timeframe), timeframe):
            return None
        return (bars[-1].close - bars[0].close) / bars[0].close

    def session_open_price(
        self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime
    ) -> Decimal:
        """Open of the first bar of the session containing the bar that
        closed at ``as_of``."""

        return self._current_session_bars(instrument_id, timeframe, as_of=as_of)[0].open

    def bars_since_session_open(
        self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime
    ) -> int:
        """Zero on the session's first closed bar."""

        return len(self._current_session_bars(instrument_id, timeframe, as_of=as_of)) - 1

    @staticmethod
    def _window_is_complete(
        bars: Sequence[Bar],
        start: datetime,
        through: datetime,
        timeframe: Timeframe,
    ) -> bool:
        step = duration(timeframe)
        expected = start
        for bar in bars:
            if bar.event_time != expected:
                return False
            expected += step
        return expected == through + step

    def _current_session_bars(
        self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime
    ) -> Sequence[Bar]:
        """Bars of the session containing the bar that closed at ``as_of``.

        Keyed off that bar's own ``event_time`` (``as_of - duration(timeframe)``),
        not off ``as_of`` itself. ``as_of`` is the closing bar's
        ``availability_time``, one timeframe later -- on a session's own
        closing bar the two name different sessions, and keying off ``as_of``
        would answer with the *next* session's window while the bar actually
        being described still belongs to the one before it.
        ``FeatureSnapshot.session`` is pinned to ``session_of(bar.event_time)``
        for the same reason, so this agrees with it by construction rather
        than by both sides remembering the same rule.

        Raises ``InsufficientHistoryError`` when the window is empty or
        missing any expected bar, when the store's coverage only starts
        partway through it, or when the reference bar falls in
        ``Session.OFF``, which has no window at all -- there is nothing
        ``session_bounds`` can answer with. A caller only asks this inside a
        window it is already in, which is a warm-up problem either way, the
        same family the guard in ``_window`` refuses for rather than
        inventing a placeholder answer.
        """

        reference = as_of - duration(timeframe)
        session = session_of(reference)
        if session is Session.OFF:
            raise InsufficientHistoryError
        start, end = session_bounds(reference, session)
        coverage = self._store.coverage(instrument_id, timeframe)
        if coverage.earliest_event_time is None or start < coverage.earliest_event_time:
            raise InsufficientHistoryError
        # Unlike prior_session_return's identical-looking filter, this one is
        # redundant here: `_read` already ends at `as_of`, and `event_time <
        # end` always holds for the reference bar's own window. Kept for
        # symmetry with prior_session_return, where the same clause is
        # load-bearing (the preceding window's `end` can sit strictly before
        # `as_of`) -- so a later reader does not delete the one that matters.
        bars = [
            bar
            for bar in self._read(instrument_id, timeframe, start=start, as_of=as_of)
            if bar.event_time < end
        ]
        if not bars:
            raise InsufficientHistoryError
        if not self._window_is_complete(bars, start, reference, timeframe):
            raise InsufficientHistoryError
        return bars

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

        A third gap the span cannot bridge: it scales with the bar count
        while a market closure does not -- a 48-hour FX weekend is wider
        than an entire M1 window, so the first attempt can land entirely
        inside it even though the store holds months of history. Rather than
        guess a multiplier large enough for the longest closure (Christmas
        will eventually exceed it), retry once from the store's earliest bar.
        The answer is still deterministic: it's always the last ``count``
        bars, whichever attempt supplied them.
        """

        if count <= 0:
            raise ValueError("count must be positive")

        coverage = self._store.coverage(instrument_id, timeframe)
        if coverage.earliest_event_time is None or coverage.latest_event_time is None:
            raise InsufficientHistoryError

        span = duration(timeframe) * count * SPAN_SAFETY
        anchor = min(as_of, coverage.latest_event_time)
        start = max(anchor - span, coverage.earliest_event_time)
        bars = self._read(instrument_id, timeframe, start=start, as_of=as_of)
        if len(bars) < count:
            bars = self._read(
                instrument_id, timeframe, start=coverage.earliest_event_time, as_of=as_of
            )
        if len(bars) < count:
            raise InsufficientHistoryError
        return bars[-count:]

    def _read(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        as_of: datetime,
    ) -> tuple[Bar, ...]:
        return self._store.bars(instrument_id, timeframe, start=start, end=as_of, as_of=as_of)
