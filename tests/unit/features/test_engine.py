from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_house.core.errors import CoverageError, InsufficientHistoryError
from trading_house.features.engine import SPAN_SAFETY, WARMUP_MULTIPLE, FeatureEngine
from trading_house.marketdata.models import Bar, BarQuality, Coverage, Timeframe, duration

BASE = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _bar(minute: int, *, spread: int = 9) -> Bar:
    """High and low swing on a fixed cycle so successive true ranges differ.

    A constant-range series makes Wilder's recursion a fixed point -- seed
    and every later value collapse to the same number -- so a determinism
    test built on one cannot distinguish window lengths at all.
    """

    opened = BASE + timedelta(minutes=minute)
    swing = Decimal(minute % 7) / Decimal(100)
    high = Decimal("1.2") + swing
    low = Decimal("1.0") - swing
    close = (high + low) / 2
    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        event_time=opened,
        availability_time=opened + timedelta(minutes=1),
        open=close,
        high=high,
        low=low,
        close=close,
        tick_volume=10,
        spread=spread,
        real_volume=0,
        quality=BarQuality.OK,
    )


class FakeStore:
    """A BarReader holding a contiguous run of M1 bars, no database."""

    def __init__(self, count: int) -> None:
        self.all = [_bar(i) for i in range(count)]
        self.requests: list[tuple[datetime, datetime, datetime]] = []

    @classmethod
    def with_a_weekend_gap(cls, *, before: int, after: int) -> "FakeStore":
        """Bars for ``before`` minutes, then a 48-hour closure, then ``after``
        more -- mimicking an FX weekend sitting in the middle of the store's
        history rather than at either end of it."""

        gap_minutes = 48 * 60
        resume = before + gap_minutes
        store = cls(0)
        store.all = [_bar(i) for i in range(before)] + [_bar(resume + i) for i in range(after)]
        return store

    @classmethod
    def ending_at(cls, newest_minute: int, *, depth: int) -> "FakeStore":
        """A store whose bars run ``newest_minute - depth + 1 .. newest_minute``.

        Lets two stores of different depth still share the same most-recent
        bars, so a determinism check compares apples to apples: the same
        window, with a different amount of history sitting behind it --
        rather than two stores whose "last N bars" are simply different
        bars, which would make the comparison fail for the wrong reason.
        """

        store = cls(0)
        store.all = [_bar(i) for i in range(newest_minute - depth + 1, newest_minute + 1)]
        return store

    def bars(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
        as_of: datetime,
        include_defective: bool = False,
    ) -> tuple[Bar, ...]:
        self.requests.append((start, end, as_of))
        if self.all and start < self.all[0].event_time:
            raise CoverageError
        return tuple(
            bar
            for bar in self.all
            if start <= bar.event_time < end and bar.availability_time <= as_of
        )

    def coverage(self, instrument_id: str, timeframe: Timeframe) -> Coverage:
        if not self.all:
            return Coverage(
                instrument_id="fx.eurusd",
                timeframe=Timeframe.M1,
                earliest_event_time=None,
                latest_event_time=None,
                latest_availability_time=None,
                clean_bars=0,
                defective_bars=0,
            )
        return Coverage(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M1,
            earliest_event_time=self.all[0].event_time,
            latest_event_time=self.all[-1].event_time,
            latest_availability_time=self.all[-1].availability_time,
            clean_bars=len(self.all),
            defective_bars=0,
        )


def _engine(count: int) -> tuple[FeatureEngine, FakeStore]:
    store = FakeStore(count)
    return FeatureEngine(store), store


LATER = BASE + timedelta(days=30)


def test_the_same_instant_gives_the_same_atr_however_much_history_is_stored() -> None:
    """THE test of this phase (I-18). Same recent bars, different depth
    behind them: an engine computing over 'whatever is stored' returns a
    different number as history accumulates, so a backtest re-run months
    later would size positions differently with no code change.
    """

    newest = 2000
    shallow = FakeStore.ending_at(newest, depth=200)
    deep = FakeStore.ending_at(newest, depth=2000)
    args = {"period": 14, "as_of": LATER}

    assert FeatureEngine(shallow).atr("fx.eurusd", Timeframe.M1, **args) == FeatureEngine(deep).atr(
        "fx.eurusd", Timeframe.M1, **args
    )


def test_the_window_is_exactly_the_period_times_the_multiple() -> None:
    """Fixed, not 'enough'. Pins the exact requested span, not just a lower
    bound -- a lower-bound assertion would still pass with SPAN_SAFETY
    deleted entirely, since any wider request clears it too."""

    engine, store = _engine(2000)

    engine.atr("fx.eurusd", Timeframe.M1, period=14, as_of=LATER)

    start, end, as_of = store.requests[-1]
    assert end == as_of == LATER

    count = 14 * WARMUP_MULTIPLE + 1
    span = duration(Timeframe.M1) * count * SPAN_SAFETY
    anchor = store.all[-1].event_time  # as_of (LATER) sits long past the store's last bar
    assert start == anchor - span


def test_a_store_one_bar_short_of_the_window_refuses() -> None:
    """Not a shorter ATR. Sizing cannot trade what it cannot size."""

    period = 14
    needed = period * WARMUP_MULTIPLE + 1
    short, _ = _engine(needed - 1)
    exact, _ = _engine(needed)

    with pytest.raises(InsufficientHistoryError):
        short.atr("fx.eurusd", Timeframe.M1, period=period, as_of=LATER)

    assert exact.atr("fx.eurusd", Timeframe.M1, period=period, as_of=LATER) > 0


def test_an_empty_store_refuses_rather_than_raising_coverage_error() -> None:
    """A key with nothing stored is a warm-up problem from the caller's side,
    not a coverage bounds violation to translate."""

    engine, _ = _engine(0)

    with pytest.raises(InsufficientHistoryError):
        engine.atr("fx.eurusd", Timeframe.M1, period=14, as_of=LATER)


def test_a_short_store_is_not_asked_for_more_than_it_holds() -> None:
    """bars() raises CoverageError when start precedes the earliest stored
    bar, so a generously wide request would blow up on a young store that
    nonetheless holds enough bars. The engine clamps to coverage first."""

    engine, store = _engine(200)

    engine.atr("fx.eurusd", Timeframe.M1, period=14, as_of=LATER)

    start, _, _ = store.requests[-1]
    assert start >= store.all[0].event_time


def test_a_feature_never_sees_a_bar_that_had_not_closed() -> None:
    """Inherited from the store, pinned here because the engine chooses the
    as_of it passes down and could get that wrong."""

    engine, store = _engine(300)

    engine.atr("fx.eurusd", Timeframe.M1, period=14, as_of=LATER)

    _, _, as_of = store.requests[-1]
    assert as_of == LATER


def test_median_spread_uses_exactly_the_requested_window() -> None:
    """Every bar carrying the same spread would make this pass against any
    window at all, so the store is built with a wide older half and a tight
    recent one: a window of 50 must see only the tight bars."""

    store = FakeStore(0)
    store.all = [_bar(i, spread=100) for i in range(200)] + [
        _bar(200 + i, spread=6) for i in range(50)
    ]
    engine = FeatureEngine(store)

    assert engine.median_spread_points(
        "fx.eurusd", Timeframe.M1, window=50, as_of=LATER
    ) == Decimal("6")
    assert engine.median_spread_points(
        "fx.eurusd", Timeframe.M1, window=250, as_of=LATER
    ) == Decimal("100")


def test_median_spread_refuses_a_window_the_store_cannot_fill() -> None:
    engine, _ = _engine(10)

    with pytest.raises(InsufficientHistoryError):
        engine.median_spread_points("fx.eurusd", Timeframe.M1, window=50, as_of=LATER)


def test_a_zero_window_is_a_caller_bug_not_a_shortage() -> None:
    """`bars[-0:]` returns the whole list, so without a guard a zero window
    silently returns a median of every bar fetched -- a real-looking number
    where the design promises a refusal."""

    engine, _ = _engine(500)

    with pytest.raises(ValueError, match="positive"):
        engine.median_spread_points("fx.eurusd", Timeframe.M1, window=0, as_of=LATER)


def test_a_weekend_gap_does_not_starve_the_window() -> None:
    """The widened span scales with the bar count; a closure does not. On M1
    a 48-hour weekend is wider than the whole window, so the first attempt
    lands inside it -- against a store holding ample history."""

    store = FakeStore.with_a_weekend_gap(before=1000, after=5)
    engine = FeatureEngine(store)
    as_of = store.all[-1].availability_time + timedelta(minutes=3)

    assert engine.atr("fx.eurusd", Timeframe.M1, period=14, as_of=as_of) > 0


@dataclass(slots=True)
class FakeBarReader:
    """A ``BarReader`` over a list held in memory.

    ``held`` is public on purpose. A test staging a store that changes its
    mind between cycles assigns to it, and a private name would accept the
    assignment silently while this fake went on serving the old bars --
    which is how a test passes for a reason unrelated to what it checks.
    """

    held: tuple[Bar, ...]

    def bars(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
        as_of: datetime,
        include_defective: bool = False,
    ) -> tuple[Bar, ...]:
        """``[start, end)`` by event time, knowable at ``as_of`` -- the real
        store's contract, including its ``CoverageError`` on a start that
        reaches further back than anything held."""

        coverage = self.coverage(instrument_id, timeframe)
        if coverage.earliest_event_time is None or start < coverage.earliest_event_time:
            raise CoverageError
        return tuple(
            bar
            for bar in sorted(self.held, key=lambda bar: bar.event_time)
            if bar.instrument_id == instrument_id
            and bar.timeframe is timeframe
            and start <= bar.event_time < end
            and bar.availability_time <= as_of
            and (include_defective or bar.quality is BarQuality.OK)
        )

    def coverage(self, instrument_id: str, timeframe: Timeframe) -> Coverage:
        matching = [
            bar
            for bar in self.held
            if bar.instrument_id == instrument_id and bar.timeframe is timeframe
        ]
        return Coverage(
            instrument_id=instrument_id,
            timeframe=timeframe,
            earliest_event_time=min((bar.event_time for bar in matching), default=None),
            latest_event_time=max((bar.event_time for bar in matching), default=None),
            latest_availability_time=max((bar.availability_time for bar in matching), default=None),
            clean_bars=sum(1 for bar in matching if bar.quality is BarQuality.OK),
            defective_bars=sum(1 for bar in matching if bar.quality is not BarQuality.OK),
        )


def _m15_bar(event_time: datetime, price: Decimal) -> Bar:
    """A flat M15 bar: opens and closes at ``price``."""

    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M15,
        event_time=event_time,
        availability_time=event_time + duration(Timeframe.M15),
        open=price,
        high=price + Decimal("0.00010"),
        low=price - Decimal("0.00010"),
        close=price,
        tick_volume=10,
        spread=9,
        real_volume=0,
        quality=BarQuality.OK,
    )


def _asian_then_london() -> tuple[Bar, ...]:
    """Two Asian M15 bars bracketing a 0.001 close-to-close return, then the
    first bar of the following London session."""

    return (
        _m15_bar(datetime(2026, 9, 21, 0, 0, tzinfo=UTC), Decimal("1.10000")),
        _m15_bar(datetime(2026, 9, 21, 6, 45, tzinfo=UTC), Decimal("1.10110")),
        _m15_bar(datetime(2026, 9, 21, 7, 0, tzinfo=UTC), Decimal("1.10120")),
    )


def _london_only() -> tuple[Bar, ...]:
    """A bar the evening before, then a gap spanning the whole Asian window,
    then London resumes -- the "data gap" case, not "nothing stored yet": a
    truly empty store fails the read with ``CoverageError`` before
    ``prior_session_return`` ever gets to decide, which is a different
    failure than the one this fixture is for."""

    return (
        _m15_bar(datetime(2026, 9, 20, 23, 45, tzinfo=UTC), Decimal("1.09990")),
        _m15_bar(datetime(2026, 9, 21, 7, 0, tzinfo=UTC), Decimal("1.10120")),
    )


def _flat_asian_then_london() -> tuple[Bar, ...]:
    """Asian closes exactly where it opened."""

    return (
        _m15_bar(datetime(2026, 9, 21, 0, 0, tzinfo=UTC), Decimal("1.10000")),
        _m15_bar(datetime(2026, 9, 21, 6, 45, tzinfo=UTC), Decimal("1.10000")),
        _m15_bar(datetime(2026, 9, 21, 7, 0, tzinfo=UTC), Decimal("1.10000")),
    )


def test_the_prior_session_return_is_close_to_close_over_the_preceding_window() -> None:
    """At the London open the preceding window is the Asian session."""

    engine = FeatureEngine(FakeBarReader(_asian_then_london()))

    value = engine.prior_session_return(
        "fx.eurusd", Timeframe.M15, as_of=datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
    )

    # Asian window first close 1.10000, last close 1.10110 -> 0.001
    assert value == Decimal("0.001")


def test_a_preceding_window_with_no_bars_returns_none_not_zero() -> None:
    """None means "no data", zero means "no movement", and a strategy must be
    able to tell them apart -- otherwise a feed outage looks like a flat
    night and the strategy silently stands down for the wrong reason."""

    engine = FeatureEngine(FakeBarReader(_london_only()))

    assert (
        engine.prior_session_return(
            "fx.eurusd", Timeframe.M15, as_of=datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
        )
        is None
    )


def test_a_flat_preceding_window_returns_zero_not_none() -> None:
    """The mirror of the case above, and the reason it cannot be one branch."""

    engine = FeatureEngine(FakeBarReader(_flat_asian_then_london()))

    assert engine.prior_session_return(
        "fx.eurusd", Timeframe.M15, as_of=datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
    ) == Decimal(0)


def test_the_session_open_price_and_bar_count_at_the_open() -> None:
    """As of 07:15 the 07:00 London bar has just closed and is the only bar
    the window can know about -- not 07:00 itself, the wall-clock instant
    the session opens: a bar opening exactly at ``as_of`` cannot yet be
    known then (``Bar`` requires ``availability_time > event_time``), so no
    bar of a brand-new window is ever readable at the window's own start."""

    engine = FeatureEngine(FakeBarReader(_asian_then_london()))
    as_of = datetime(2026, 9, 21, 7, 15, tzinfo=UTC)

    assert engine.session_open_price("fx.eurusd", Timeframe.M15, as_of=as_of) == Decimal("1.10120")
    assert engine.bars_since_session_open("fx.eurusd", Timeframe.M15, as_of=as_of) == 0
