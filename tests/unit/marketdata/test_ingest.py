"""Backfill and update: the orchestration a run ledger makes accountable.

``FakeProvider`` returns canned pages and records what was asked; ``FakeStore``
records what was appended and reports a configurable ``Coverage``. Neither
touches a database or a broker -- the whole point of testing the walk and the
outcome derivation in isolation from both.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from trading_house.brokers.mt5.boundary import Mt5Bar
from trading_house.core.clock import FixedClock
from trading_house.core.errors import BrokerUnavailableError
from trading_house.marketdata.ingest import _last_completed_boundary, backfill, update
from trading_house.marketdata.models import Bar, Coverage, IngestOutcome, Timeframe
from trading_house.marketdata.sessions import is_liquid
from trading_house.marketdata.store import WriteResult

NINE = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
"""Wednesday 09:00 UTC -- fully liquid, and the base every test bar's minute
offset is measured from."""

FIXED_CLOCK = FixedClock(datetime(2026, 8, 26, 10, 0, tzinfo=UTC))
"""One liquid hour after NINE, already aligned to an M1 boundary."""

_INSTRUMENT_ID = "fx.eurusd"
PLUS_THREE = timezone(timedelta(hours=3))

BACKFILL_ARGS: dict[str, object] = {
    "instrument_id": _INSTRUMENT_ID,
    "timeframe": Timeframe.M1,
    "until": datetime(2026, 2, 26, 10, 0, tzinfo=UTC),  # ~six months before FIXED_CLOCK
    "server_zone": UTC,
}

ONE_HOUR_BACKFILL: dict[str, object] = {
    "instrument_id": _INSTRUMENT_ID,
    "timeframe": Timeframe.M1,
    "until": NINE,
    "server_zone": UTC,
}

UPDATE_ARGS: dict[str, object] = {
    "instrument_id": _INSTRUMENT_ID,
    "timeframe": Timeframe.M1,
    "until": datetime(2026, 8, 25, 8, 0, tzinfo=UTC),
    "server_zone": UTC,
}


def _mt5_bar(minute: int, **overrides: object) -> Mt5Bar:
    """A coherent bar opening ``minute`` minutes after ``NINE``.

    ``FakeProvider`` ignores the window it is asked for and returns whatever
    page was canned, so these timestamps never need to line up with a real
    request window -- only with each other.
    """

    kwargs: dict[str, object] = {
        "event_time": NINE + timedelta(minutes=minute),
        "open": 1.10000,
        "high": 1.10030,
        "low": 1.09990,
        "close": 1.10020,
        "tick_volume": 100,
        "spread": 10,
        "real_volume": 0,
    }
    kwargs.update(overrides)
    return Mt5Bar(**kwargs)  # type: ignore[arg-type]


def _full_liquid_page() -> list[Mt5Bar]:
    """One bar per minute across a liquid hour."""

    return [_mt5_bar(i) for i in range(60)]


def _no_pause(_seconds: float) -> None:
    """Injected in place of the real ``time.sleep`` default so a test that
    triggers a retry never actually waits."""


class FakeProvider:
    """Returns canned pages, newest request first, and records what was asked."""

    def __init__(self, pages: list[list[Mt5Bar]]) -> None:
        self._pages = pages
        self.requests: list[tuple[datetime, datetime]] = []

    def history(
        self, instrument_id: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Sequence[Mt5Bar]:
        self.requests.append((start, end))
        return self._pages.pop(0) if self._pages else []


class FakeStore:
    """Records appended bars and reports a configurable, fixed ``Coverage``."""

    def __init__(
        self,
        *,
        earliest_event_time: datetime | None = None,
        latest_event_time: datetime | None = None,
    ) -> None:
        self._earliest_event_time = earliest_event_time
        self._latest_event_time = latest_event_time
        self.appended: list[Bar] = []
        self.finalized: list[tuple[UUID, int, int, IngestOutcome]] = []

    def record_run(self, run: object) -> None:
        pass

    def append_bars(self, bars: Sequence[Bar], run_id: UUID) -> WriteResult:
        self.appended.extend(bars)
        return WriteResult(stored=len(bars), duplicate=0, conflicting=0)

    def finalize_run(
        self,
        run_id: UUID,
        *,
        bars_stored: int,
        bars_conflicting: int,
        outcome: IngestOutcome,
        finished_at: datetime,
    ) -> None:
        self.finalized.append((run_id, bars_stored, bars_conflicting, outcome))

    def bars(self, *args: object, **kwargs: object) -> tuple[Bar, ...]:
        return ()

    def coverage(self, instrument_id: str, timeframe: Timeframe) -> Coverage:
        return Coverage(
            instrument_id=instrument_id,
            timeframe=timeframe,
            earliest_event_time=self._earliest_event_time,
            latest_event_time=self._latest_event_time,
            latest_availability_time=None,
            clean_bars=0,
            defective_bars=0,
        )


def test_an_empty_page_is_retried_once_before_being_believed() -> None:
    """MT5 downloads history lazily, so a first deep request genuinely can
    return less than a second one. Concluding exhaustion on a single empty
    answer would leave real history permanently unfetched."""

    provider = FakeProvider([[], [_mt5_bar(0)]])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, pause=_no_pause, **BACKFILL_ARGS)

    assert len(provider.requests) >= 2
    assert run.bars_returned == 1


def test_the_single_bar_artifact_counts_as_exhaustion_not_as_data() -> None:
    """Measured on FBS: asking for six-month-old M1 returns exactly one bar
    and no error. Storing that and reporting success is the failure this
    entire phase exists to prevent."""

    provider = FakeProvider([[_mt5_bar(0)], [_mt5_bar(0)]])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, pause=_no_pause, **BACKFILL_ARGS)

    assert run.outcome in {IngestOutcome.TRUNCATED, IngestOutcome.EMPTY}


def test_hitting_the_depth_wall_is_truncated_not_complete() -> None:
    """TRUNCATED and COMPLETE differ by whether the requested start was
    reached. Collapsing them would make a backtest unable to tell a full
    history from a wall."""

    provider = FakeProvider([[_mt5_bar(i) for i in range(50)], [], []])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, pause=_no_pause, **BACKFILL_ARGS)

    assert run.outcome is IngestOutcome.TRUNCATED
    assert run.earliest_event_time is not None


def _m15_bars(start: datetime, end: datetime) -> list[Mt5Bar]:
    """One M15 bar per liquid quarter-hour across ``[start, end)``."""

    count = int((end - start) / timedelta(minutes=15))
    times = (start + timedelta(minutes=15 * i) for i in range(count))
    return [_mt5_bar(0, event_time=at) for at in times if is_liquid(at)]


TWO_WEEK_M15_BACKFILL: dict[str, object] = {
    "instrument_id": _INSTRUMENT_ID,
    "timeframe": Timeframe.M15,
    "until": FIXED_CLOCK.now() - timedelta(days=14),
    "server_zone": UTC,
}


def test_a_full_page_that_starts_days_late_is_truncated_not_complete() -> None:
    """MT5's "Max bars in chart" cap (100,000 by default) is a depth wall that
    returns a full page, not an empty one: on 2026-10-04 a backfill to
    2022-09-16 came back with 99,999 bars starting 2022-09-23 16:30 and was
    graded COMPLETE on its 0.99 coverage. Two days of liquid market missing
    before the first bar means the requested start was never reached."""

    late = FIXED_CLOCK.now() - timedelta(days=12)
    provider = FakeProvider([_m15_bars(late, FIXED_CLOCK.now())])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, pause=_no_pause, **TWO_WEEK_M15_BACKFILL)

    assert run.coverage_ratio >= Decimal("0.5")
    assert run.outcome is IngestOutcome.TRUNCATED


def test_a_holiday_at_the_start_of_the_window_still_reaches_it() -> None:
    """A market holiday on the window's first day leaves up to a day of liquid
    time with no bars, and that is the market, not a wall."""

    late = FIXED_CLOCK.now() - timedelta(days=13)
    provider = FakeProvider([_m15_bars(late, FIXED_CLOCK.now())])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, pause=_no_pause, **TWO_WEEK_M15_BACKFILL)

    assert run.outcome is IngestOutcome.COMPLETE


def test_a_wall_months_after_the_start_is_truncated_without_counting_every_step() -> None:
    """Six months of M1 is more steps than ``expected_bars`` will walk, so the
    leading gap must be judged without counting all of it."""

    provider = FakeProvider([[_mt5_bar(0), _mt5_bar(1)] for _ in range(20)])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, pause=_no_pause, **BACKFILL_ARGS)

    assert run.outcome is IngestOutcome.TRUNCATED


def test_reaching_the_requested_start_with_full_coverage_is_complete() -> None:
    provider = FakeProvider([_full_liquid_page()])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, pause=_no_pause, **ONE_HOUR_BACKFILL)

    assert run.outcome is IngestOutcome.COMPLETE
    assert run.coverage_ratio >= Decimal("0.5")


def test_reaching_the_start_with_thin_data_is_sparse_not_complete() -> None:
    """The distinction that stops a half-empty window passing as success."""

    provider = FakeProvider([_full_liquid_page()[:5]])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, pause=_no_pause, **ONE_HOUR_BACKFILL)

    assert run.outcome is IngestOutcome.SPARSE


def test_the_run_records_how_far_back_it_actually_reached() -> None:
    """earliest_event_time is the fact a backtest needs and can obtain
    nowhere else."""

    bars = [_mt5_bar(i) for i in range(10)]
    provider = FakeProvider([bars, []])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, pause=_no_pause, **BACKFILL_ARGS)

    assert run.earliest_event_time == min(b.event_time for b in bars)


def test_defective_bars_are_counted_and_still_stored() -> None:
    """Rejected by the gates, kept by the store (D-2)."""

    store = FakeStore()
    provider = FakeProvider([[_mt5_bar(0), _mt5_bar(1, high=0.5)]])

    run = backfill(provider, store, FIXED_CLOCK, pause=_no_pause, **ONE_HOUR_BACKFILL)

    assert run.bars_rejected == 1
    assert len(store.appended) == 2


def test_update_starts_from_the_newest_stored_bar() -> None:
    """Not from the beginning of time. The stored data is the cursor."""

    store = FakeStore(latest_event_time=NINE)
    provider = FakeProvider([[]])

    update(provider, store, FIXED_CLOCK, pause=_no_pause, **UPDATE_ARGS)

    assert provider.requests[0][0] >= NINE


def test_the_forming_bar_is_never_requested() -> None:
    """Bar 0 is incomplete. Bounding the request at the last completed
    boundary is what stops a partial bar being stored as a closed one, and
    silently look-ahead-leaking into every backtest that reads it."""

    provider = FakeProvider([[]])
    now = datetime(2026, 8, 25, 9, 30, 45, tzinfo=UTC)

    update(provider, FakeStore(), FixedClock(now), pause=_no_pause, **UPDATE_ARGS)

    _, end = provider.requests[0]
    assert end <= datetime(2026, 8, 25, 9, 30, tzinfo=UTC)


def test_a_caught_up_update_reports_complete_not_truncated() -> None:
    """update()'s resting state. A page expecting one bar and receiving one
    has full coverage; calling that exhaustion makes the outcome field least
    trustworthy on the path that runs most often."""

    store = FakeStore(latest_event_time=NINE + timedelta(minutes=59))
    provider = FakeProvider([[_mt5_bar(0)]])

    run = update(provider, store, FIXED_CLOCK, pause=_no_pause, **UPDATE_ARGS)

    assert run.outcome is IngestOutcome.COMPLETE


def test_a_caught_up_update_does_not_pause() -> None:
    """The retry exists for a suspicious page. A page that returned exactly
    what it expected is not suspicious, and sleeping on it burns a real
    second per poll."""

    calls: list[float] = []

    store = FakeStore(latest_event_time=NINE + timedelta(minutes=59))
    provider = FakeProvider([[_mt5_bar(0)]])

    update(provider, store, FIXED_CLOCK, pause=calls.append, **UPDATE_ARGS)

    assert calls == []


def test_nothing_to_fetch_is_complete_not_empty() -> None:
    """No pages planned means the store is already current. EMPTY is for a
    plan that was executed and yielded nothing."""

    store = FakeStore(latest_event_time=FIXED_CLOCK.now())
    provider = FakeProvider([])

    run = update(provider, store, FIXED_CLOCK, pause=_no_pause, **UPDATE_ARGS)

    assert run.outcome is IngestOutcome.COMPLETE
    assert len(provider.requests) == 0


# --- the run ledger's detail must never quote an exception's own text -------


def test_a_failed_run_records_the_kind_of_failure_not_its_message() -> None:
    """`IngestRun.detail` is stored in the ledger and printed to stdout by the
    CLI on an otherwise successful response, so it bypasses the redaction the
    CLI applies to errors it raises itself. A bare `str(error)` there would be
    an unbounded channel for anything a future exception happens to carry."""

    class _ExplodingProvider:
        def history(self, instrument_id, timeframe, start, end):  # type: ignore[no-untyped-def]
            raise RuntimeError("dsn=postgresql://user:hunter2@db/trading account=106231964")

    run = backfill(_ExplodingProvider(), FakeStore(), FIXED_CLOCK, **BACKFILL_ARGS)

    assert run.outcome is IngestOutcome.FAILED
    assert run.detail == "RuntimeError"
    assert "hunter2" not in (run.detail or "")
    assert "106231964" not in (run.detail or "")


def test_last_completed_boundary_floors_in_the_brokers_frame_not_utc() -> None:
    """FBS is UTC+3: its H4 candles open at 21:00/01:00/05:00 UTC and its D1
    opens at 21:00 UTC, none of which divide their period in raw UTC seconds.
    Flooring the UTC epoch directly lands the cutoff inside the still-forming
    candle instead of at its true, closed boundary."""

    instant = datetime(2026, 8, 26, 0, 30, tzinfo=UTC)

    h4 = _last_completed_boundary(Timeframe.H4, instant, server_zone=PLUS_THREE)
    d1 = _last_completed_boundary(Timeframe.D1, instant, server_zone=PLUS_THREE)

    assert h4 == datetime(2026, 8, 25, 21, 0, tzinfo=UTC)
    assert d1 == datetime(2026, 8, 25, 21, 0, tzinfo=UTC)


def test_last_completed_boundary_floors_at_the_instants_own_seasons_offset() -> None:
    """In January an EU-rules broker is UTC+2, so its D1 candle opens at
    22:00 UTC, not the 21:00 UTC of summer."""

    athens = ZoneInfo("Europe/Athens")
    instant = datetime(2026, 1, 15, 0, 30, tzinfo=UTC)

    d1 = _last_completed_boundary(Timeframe.D1, instant, server_zone=athens)

    assert d1 == datetime(2026, 1, 14, 22, 0, tzinfo=UTC)


def test_update_walks_newest_first_so_a_fresh_key_still_fetches_recent_pages() -> None:
    """A fresh key's plan can span far more pages than the broker actually
    holds history for -- the CLI's genesis fallback plans M1 page one in
    1970, decades before any real data. Walking that plan oldest-first would
    exhaust on page one and never even attempt the recent pages that hold
    real bars."""

    genesis = datetime(1970, 1, 1, tzinfo=UTC)
    wall = FIXED_CLOCK.now() - timedelta(days=90)
    bars = [_mt5_bar(i) for i in range(5)]

    class _WallLimitedProvider:
        def __init__(self) -> None:
            self.requests: list[tuple[datetime, datetime]] = []

        def history(
            self, instrument_id: str, timeframe: Timeframe, start: datetime, end: datetime
        ) -> Sequence[Mt5Bar]:
            self.requests.append((start, end))
            return [] if end <= wall else bars

    provider = _WallLimitedProvider()

    run = update(
        provider,
        FakeStore(),
        FIXED_CLOCK,
        pause=_no_pause,
        instrument_id=_INSTRUMENT_ID,
        timeframe=Timeframe.M1,
        until=genesis,
        server_zone=UTC,
    )

    assert run.bars_returned > 0
    assert run.outcome is not IngestOutcome.EMPTY


def test_an_inverted_backfill_range_is_a_no_op_not_a_broken_run() -> None:
    """``--from`` later than what is already stored produces requested_from >
    requested_to, which no Python validator catches and which the run
    ledger's ``runs_range_ordered`` CHECK rejects. Treating it as a no-op
    keeps that inverted range from ever reaching the insert."""

    earliest = datetime(2026, 6, 1, tzinfo=UTC)
    later_until = earliest + timedelta(days=1)
    store = FakeStore(earliest_event_time=earliest)
    provider = FakeProvider([])

    run = backfill(
        provider,
        store,
        FIXED_CLOCK,
        pause=_no_pause,
        instrument_id=_INSTRUMENT_ID,
        timeframe=Timeframe.M1,
        until=later_until,
        server_zone=UTC,
    )

    assert run.outcome is IngestOutcome.COMPLETE
    assert run.bars_returned == 0
    assert run.requested_from == run.requested_to
    assert len(provider.requests) == 0


def test_a_typed_failure_contributes_its_fixed_public_message() -> None:
    """A TradingHouseError's message is a class constant with no interpolated
    content, so it is safe to surface and says more than the class name."""

    class _UnavailableProvider:
        def history(self, instrument_id, timeframe, start, end):  # type: ignore[no-untyped-def]
            raise BrokerUnavailableError

    run = backfill(_UnavailableProvider(), FakeStore(), FIXED_CLOCK, **BACKFILL_ARGS)

    assert run.detail == "BrokerUnavailableError: broker terminal unavailable"
