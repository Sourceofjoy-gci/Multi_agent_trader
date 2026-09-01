"""Backfill and update: the orchestration that drives everything else.

Asking MetaTrader 5 for six-month-old M1 history returns one bar and no
error -- a successful call reporting almost nothing. A naive backfill would
store that bar, report success, and every backtest afterwards would run on a
window it did not choose and cannot see. This module exists to make that
outcome loud instead of silent: every run is recorded, with an outcome that
distinguishes a full history from a wall it hit partway through, and an
``earliest_event_time`` that tells a backtest exactly how far back the data
actually goes.

The stored data is the cursor. ``backfill`` resumes from
``store.coverage().earliest_event_time`` and walks backwards toward
``until``; ``update`` resumes from ``latest_event_time`` and walks forward
toward now. There is no separate cursor table -- the bars already on disk
are the only bookmark either function needs.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from trading_house.brokers.mt5.boundary import Mt5Bar
from trading_house.brokers.mt5.contracts import decimal_of
from trading_house.core.clock import Clock
from trading_house.marketdata.models import (
    Bar,
    BarQuality,
    IngestOutcome,
    IngestRun,
    Timeframe,
    availability_of,
    duration,
)
from trading_house.marketdata.paging import plan_backward
from trading_house.marketdata.provider import HistoryProvider
from trading_house.marketdata.quality import assess
from trading_house.marketdata.sessions import expected_bars
from trading_house.marketdata.store import BarStore, WriteResult

Pause = Callable[[float], None]
"""A retry delay, injected so tests never need a real sleep. Defaults to
``time.sleep`` -- a caller who forgets to override it gets a real pause
between retries rather than a hot loop hammering the broker."""

_RETRY_PAUSE_SECONDS = 1.0
_COMPLETE_RATIO = Decimal("0.5")

_NO_WRITE = WriteResult(stored=0, duplicate=0, conflicting=0)
"""What a run that fetched nothing handed to the store: nothing."""


def _last_completed_boundary(timeframe: Timeframe, instant: datetime) -> datetime:
    """The last completed bar boundary at or before ``instant``.

    A bar opening at that boundary has not closed yet, so truncating the
    newest bound of any request to this value is what stops a partial bar
    being stored as a closed one and look-ahead-leaking into every backtest
    that reads it.
    """

    step_seconds = int(duration(timeframe).total_seconds())
    epoch_seconds = int(instant.timestamp())
    floored = (epoch_seconds // step_seconds) * step_seconds
    return datetime.fromtimestamp(floored, tz=UTC)


@dataclass(frozen=True, slots=True)
class _PageFetch:
    bars: Sequence[Mt5Bar]
    exhausted: bool


def _looks_exhausted(returned: int, expected: int) -> bool:
    """Whether a page's answer looks like the broker has nothing more to give.

    This cannot be an absolute bar count: the two directions of travel want
    opposite readings of the same ``len(bars) <= 1``. Walking backward into
    deep history, a page asking for 20,000 bars and getting 1 is the
    broker's depth wall (Ruling 2 -- the single-bar artifact). Walking
    forward in ``update()``, a page asking for 1 bar -- the ordinary shape
    of a poll run more often than once per bar interval -- and getting 1
    back is a fully caught-up feed, not a wall. Judging exhaustion relative
    to what the page itself expected is what tells these apart: zero bars
    is always suspicious, but one bar is only suspicious when more were
    expected.
    """

    return returned == 0 or (returned <= 1 and expected > 1)


def _fetch_page(
    provider: HistoryProvider,
    instrument_id: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    expected: int,
    pause: Pause,
) -> _PageFetch:
    """Fetch one page, retrying once on an answer that looks exhausted.

    The retry re-asks the identical window rather than adding to the first
    answer: a second attempt either reveals the history the first attempt's
    lazy download had not fetched yet, or confirms there is nothing more to
    find. Either way the retry's own answer is what the page holds -- it
    replaces the first attempt rather than being added to it.
    """

    bars = provider.history(instrument_id, timeframe, start, end)
    if not _looks_exhausted(len(bars), expected):
        return _PageFetch(bars=bars, exhausted=False)

    pause(_RETRY_PAUSE_SECONDS)
    retried = provider.history(instrument_id, timeframe, start, end)
    if not _looks_exhausted(len(retried), expected):
        return _PageFetch(bars=retried, exhausted=False)
    return _PageFetch(bars=retried, exhausted=True)


def _to_bar(
    raw: Mt5Bar, instrument_id: str, timeframe: Timeframe, server_offset_seconds: int
) -> Bar:
    """Convert one broker bar to canonical form and grade it (D-2 keeps a
    defective bar rather than dropping it, so this never raises on a bad
    price -- it only classifies)."""

    bar_open = decimal_of(raw.open)
    high = decimal_of(raw.high)
    low = decimal_of(raw.low)
    close = decimal_of(raw.close)
    quality = assess(
        bar_open=bar_open,
        high=high,
        low=low,
        close=close,
        spread=raw.spread,
        timeframe=timeframe,
        event_time=raw.event_time,
        server_offset_seconds=server_offset_seconds,
    )
    return Bar(
        instrument_id=instrument_id,
        timeframe=timeframe,
        event_time=raw.event_time,
        availability_time=availability_of(timeframe, raw.event_time),
        open=bar_open,
        high=high,
        low=low,
        close=close,
        tick_volume=raw.tick_volume,
        spread=raw.spread,
        real_volume=raw.real_volume,
        quality=quality,
    )


@dataclass(frozen=True, slots=True)
class _WalkResult:
    bars: tuple[Bar, ...]
    had_pages: bool
    reached_target: bool
    expected: int
    failed: bool
    detail: str | None


def _walk(
    provider: HistoryProvider,
    instrument_id: str,
    timeframe: Timeframe,
    server_offset_seconds: int,
    pages: Sequence[tuple[datetime, datetime]],
    pause: Pause,
) -> _WalkResult:
    """Fetch every page in order, stopping the moment one is exhausted.

    ``expected`` is accumulated per page (Ruling 1): ``sessions.expected_bars``
    raises above 200,000 walk steps, and a whole requested range can run to
    millions of minutes, so it is only ever asked about one page -- bounded
    by ``PAGE_BARS`` -- at a time. Only pages actually queried count toward
    it, which is exactly what makes the resulting ratio mean "what came back
    against what those pages should have held" rather than something a caller
    never actually asked for.
    """

    bars: list[Bar] = []
    expected_total = 0
    reached_target = True
    had_pages = bool(pages)
    try:
        for start, end in pages:
            expected = expected_bars(timeframe, start, end)
            fetch = _fetch_page(provider, instrument_id, timeframe, start, end, expected, pause)
            expected_total += expected
            bars.extend(
                _to_bar(raw, instrument_id, timeframe, server_offset_seconds) for raw in fetch.bars
            )
            if fetch.exhausted:
                reached_target = False
                break
    except Exception as error:  # the run must be recorded even when the fetch itself errors
        return _WalkResult(
            bars=tuple(bars),
            had_pages=had_pages,
            reached_target=False,
            expected=expected_total,
            failed=True,
            detail=str(error),
        )
    return _WalkResult(
        bars=tuple(bars),
        had_pages=had_pages,
        reached_target=reached_target,
        expected=expected_total,
        failed=False,
        detail=None,
    )


def _derive_outcome(
    *,
    failed: bool,
    had_pages: bool,
    bars_returned: int,
    reached_target: bool,
    coverage_ratio: Decimal,
) -> IngestOutcome:
    if failed:
        return IngestOutcome.FAILED
    if not had_pages:
        # Nothing was planned because the store was already current -- not
        # because a plan was executed and came back with nothing. Those are
        # different facts, and EMPTY is reserved for the second one.
        return IngestOutcome.COMPLETE if reached_target else IngestOutcome.TRUNCATED
    if bars_returned == 0:
        return IngestOutcome.EMPTY
    if not reached_target:
        return IngestOutcome.TRUNCATED
    if coverage_ratio >= _COMPLETE_RATIO:
        return IngestOutcome.COMPLETE
    return IngestOutcome.SPARSE


def _finish(
    *,
    store: BarStore,
    instrument_id: str,
    timeframe: Timeframe,
    requested_from: datetime,
    requested_to: datetime,
    started_at: datetime,
    clock: Clock,
    walk: _WalkResult,
) -> IngestRun:
    """Derive the outcome, then record, write, and finalize in that order.

    ``record_run`` must precede ``append_bars`` -- a bar's ``ingest_run_id``
    is a foreign key to the run row -- so the initial insert carries
    provisional ``bars_stored``/``bars_conflicting`` of zero: whether a
    fetched bar actually lands as a fresh row, a benign duplicate of one a
    *previous* run already wrote, or a genuine conflict with one, is not
    knowable until ``append_bars`` has run against this exact table. Once it
    has, ``finalize_run`` completes the record with the true numbers --
    completing a still-open record, not rewriting a closed one, which is
    exactly what the runtime role's column-scoped UPDATE grant (migration
    0003) permits and nothing more. ``outcome``, by contrast, depends only on
    what was fetched and graded, never on the write, so it is correct from
    the first insert; it is passed to ``finalize_run`` again only because
    that call also carries the run's true ``finished_at``.
    """

    bars_returned = len(walk.bars)
    bars_rejected = sum(1 for bar in walk.bars if bar.quality is not BarQuality.OK)
    earliest_event_time = min((bar.event_time for bar in walk.bars), default=None)
    coverage_ratio = (
        Decimal(bars_returned) / Decimal(walk.expected) if walk.expected > 0 else Decimal("1")
    )
    outcome = _derive_outcome(
        failed=walk.failed,
        had_pages=walk.had_pages,
        bars_returned=bars_returned,
        reached_target=walk.reached_target,
        coverage_ratio=coverage_ratio,
    )

    run = IngestRun(
        run_id=uuid4(),
        instrument_id=instrument_id,
        timeframe=timeframe,
        requested_from=requested_from,
        requested_to=requested_to,
        started_at=started_at,
        finished_at=clock.now(),
        earliest_event_time=earliest_event_time,
        bars_returned=bars_returned,
        bars_stored=0,
        bars_rejected=bars_rejected,
        bars_conflicting=0,
        expected_bars=walk.expected,
        coverage_ratio=coverage_ratio,
        outcome=outcome,
        detail=walk.detail,
    )
    store.record_run(run)

    write_result = store.append_bars(walk.bars, run.run_id) if walk.bars else _NO_WRITE
    finished_at = clock.now()
    store.finalize_run(
        run.run_id,
        bars_stored=write_result.stored,
        bars_conflicting=write_result.conflicting,
        outcome=outcome,
        finished_at=finished_at,
    )
    return run.model_copy(
        update={
            "bars_stored": write_result.stored,
            "bars_conflicting": write_result.conflicting,
            "finished_at": finished_at,
        }
    )


def backfill(
    provider: HistoryProvider,
    store: BarStore,
    clock: Clock,
    *,
    instrument_id: str,
    timeframe: Timeframe,
    until: datetime,
    server_offset_seconds: int,
    pause: Pause = time.sleep,
) -> IngestRun:
    """Walk backward toward ``until``, from what is already stored or now.

    A key with existing coverage resumes from its earliest stored bar and
    keeps walking back -- the stored data is the cursor, there is no other
    bookmark. A fresh key starts from ``now``, truncated to the last
    completed boundary so the still-forming bar is never requested.
    """

    started_at = clock.now()
    coverage = store.coverage(instrument_id, timeframe)
    newest = coverage.earliest_event_time or _last_completed_boundary(timeframe, started_at)

    pages = plan_backward(timeframe, newest=newest, oldest=until) if until < newest else ()

    walk = _walk(provider, instrument_id, timeframe, server_offset_seconds, pages, pause)

    return _finish(
        store=store,
        instrument_id=instrument_id,
        timeframe=timeframe,
        requested_from=until,
        requested_to=newest,
        started_at=started_at,
        clock=clock,
        walk=walk,
    )


def update(
    provider: HistoryProvider,
    store: BarStore,
    clock: Clock,
    *,
    instrument_id: str,
    timeframe: Timeframe,
    until: datetime,
    server_offset_seconds: int,
    pause: Pause = time.sleep,
) -> IngestRun:
    """Walk forward toward now, from the newest stored bar or ``until``.

    An established key resumes from its latest stored bar -- again, the
    stored data is the cursor. A key with nothing stored yet starts from
    ``until``, the same fallback boundary ``backfill`` uses for how far back
    the window is meant to reach.
    """

    started_at = clock.now()
    coverage = store.coverage(instrument_id, timeframe)
    oldest = coverage.latest_event_time or until
    target = _last_completed_boundary(timeframe, started_at)

    pages = (
        tuple(reversed(plan_backward(timeframe, newest=target, oldest=oldest)))
        if oldest < target
        else ()
    )

    walk = _walk(provider, instrument_id, timeframe, server_offset_seconds, pages, pause)

    return _finish(
        store=store,
        instrument_id=instrument_id,
        timeframe=timeframe,
        requested_from=oldest,
        requested_to=target,
        started_at=started_at,
        clock=clock,
        walk=walk,
    )
