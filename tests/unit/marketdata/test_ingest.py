"""Backfill and update: the orchestration a run ledger makes accountable.

``FakeProvider`` returns canned pages and records what was asked; ``FakeStore``
records what was appended and reports a configurable ``Coverage``. Neither
touches a database or a broker -- the whole point of testing the walk and the
outcome derivation in isolation from both.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from trading_house.brokers.mt5.boundary import Mt5Bar
from trading_house.core.clock import FixedClock
from trading_house.marketdata.ingest import backfill, update
from trading_house.marketdata.models import Bar, Coverage, IngestOutcome, Timeframe
from trading_house.marketdata.store import WriteResult

NINE = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
"""Wednesday 09:00 UTC -- fully liquid, and the base every test bar's minute
offset is measured from."""

FIXED_CLOCK = FixedClock(datetime(2026, 8, 26, 10, 0, tzinfo=UTC))
"""One liquid hour after NINE, already aligned to an M1 boundary."""

_INSTRUMENT_ID = "fx.eurusd"

BACKFILL_ARGS: dict[str, object] = {
    "instrument_id": _INSTRUMENT_ID,
    "timeframe": Timeframe.M1,
    "until": datetime(2026, 2, 26, 10, 0, tzinfo=UTC),  # ~six months before FIXED_CLOCK
    "server_offset_seconds": 0,
}

ONE_HOUR_BACKFILL: dict[str, object] = {
    "instrument_id": _INSTRUMENT_ID,
    "timeframe": Timeframe.M1,
    "until": NINE,
    "server_offset_seconds": 0,
}

UPDATE_ARGS: dict[str, object] = {
    "instrument_id": _INSTRUMENT_ID,
    "timeframe": Timeframe.M1,
    "until": datetime(2026, 8, 25, 8, 0, tzinfo=UTC),
    "server_offset_seconds": 0,
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
