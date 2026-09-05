# Phase 2 — Feature Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute ATR and median spread from stored bars over a fixed lookback, so the same instant always yields the same number, and refuse rather than guess when history is short.

**Architecture:** Pure indicator functions take a sequence of bars and return a `Decimal`. `FeatureEngine` holds the bar store, fetches a deterministic window ending at `as_of`, and calls them. Only the engine holds the store, so no caller can obtain a feature without a point-in-time read.

**Tech Stack:** Python 3.12, Pydantic v2, `Decimal` for all values, pytest with Hypothesis, uv.

**Spec:** `docs/superpowers/specs/2026-09-02-phase-2-feature-engine-design.md`

## Global Constraints

- **Every `uv` command must be prefixed `UV_SYSTEM_CERTS=1`** or it fails TLS verification in this environment.
- The mypy gate is `UV_SYSTEM_CERTS=1 uv run mypy` with **no path arguments** — the project scopes it via `packages = ["trading_house"]`. Passing paths hits an unrelated pre-existing basename collision.
- **`features/` imports `marketdata/` and nothing else from this project** — not `brokers/`, not `risk/`. An acceptance test pins it.
- Line length 100. mypy strict. **No new `# type: ignore` in `src/`.**
- **Do not add a `# noqa` for a rule this project does not enable.** The ruff select list is exactly `["E", "F", "I", "B", "UP", "SIM", "RUF", "S", "PT"]`. A directive for anything outside it suppresses nothing and `RUF100` fails the build. This defect class has cost this project four rounds across earlier phases.
- All values are `Decimal`. A float must never reach a feature value — it flows into stop distance and then lot size.
- The baseline suite is **840 passed / 3 skipped at 98.00% coverage**. Run `UV_SYSTEM_CERTS=1 uv run pytest -q` before committing; anything less is a regression.

## What Already Exists

```python
# trading_house.marketdata.models
class Bar(CanonicalModel):
    instrument_id: InstrumentId
    timeframe: Timeframe
    event_time: datetime
    availability_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: NonNegativeInt
    spread: int              # points, as MT5 reports
    real_volume: NonNegativeInt
    quality: BarQuality

class Coverage(CanonicalModel):
    earliest_event_time: datetime | None
    latest_event_time: datetime | None
    latest_availability_time: datetime | None
    clean_bars: NonNegativeInt
    defective_bars: NonNegativeInt

def duration(timeframe: Timeframe) -> timedelta

# trading_house.marketdata.store — the BarStore protocol
def bars(instrument_id: str, timeframe: Timeframe, *, start: datetime,
         end: datetime, as_of: datetime,
         include_defective: bool = False) -> tuple[Bar, ...]
def coverage(instrument_id: str, timeframe: Timeframe) -> Coverage
```

`bars()` returns clean bars ordered by `event_time`, filtered `availability_time <= as_of`, and **raises `CoverageError` when `start` precedes `earliest_event_time`**.

## File Structure

| File | Responsibility |
|---|---|
| `features/indicators/volatility.py` | `true_range`, `wilder_atr` — pure |
| `features/indicators/spread.py` | `median_spread_points` — pure |
| `features/engine.py` | `FeatureEngine` — window arithmetic, warm-up refusal |
| `core/errors.py` | `InsufficientHistoryError` |

---

### Task 1: Pure Indicators and the Typed Refusal

**Files:**
- Create: `src/trading_house/features/__init__.py`, `src/trading_house/features/indicators/__init__.py`, `src/trading_house/features/indicators/volatility.py`, `src/trading_house/features/indicators/spread.py`
- Modify: `src/trading_house/core/errors.py`
- Test: `tests/unit/features/test_volatility.py`, `tests/unit/features/test_spread.py`

**Interfaces:**
- Produces: `true_range(high, low, previous_close) -> Decimal`, `wilder_atr(bars, period) -> Decimal`, `median_spread_points(bars) -> Decimal`, `InsufficientHistoryError`
- Consumes: `Bar` from `marketdata.models`; `TradingHouseError` from `core.errors`

- [ ] **Step 1: Add the typed error**

Append to `src/trading_house/core/errors.py`, following the existing class shape exactly:

```python
class InsufficientHistoryError(TradingHouseError):
    """Raised when a feature needs more history than the store holds."""

    public_message = "insufficient history to compute the feature"
```

No `ExitCode`. No CLI command surfaces it in this phase; Phase 3 decides how sizing reports it.

- [ ] **Step 2: Write the failing volatility tests**

```python
# tests/unit/features/test_volatility.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.indicators.volatility import true_range, wilder_atr
from trading_house.marketdata.models import Bar, BarQuality, Timeframe

BASE = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _bar(minute: int, high: str, low: str, close: str) -> Bar:
    opened = BASE + timedelta(minutes=minute)
    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        event_time=opened,
        availability_time=opened + timedelta(minutes=1),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        tick_volume=10,
        spread=9,
        real_volume=0,
        quality=BarQuality.OK,
    )


# Worked by hand from Wilder's definition, so the expected values below are
# verifiable without running the implementation:
#
#   bar  high  low  close    true range
#   b0    10    10    10     -- (no previous close)
#   b1    12     8    11     max(12-8, |12-10|, |8-10|)  = 4
#   b2    13    11    12     max(13-11, |13-11|, |11-11|) = 2
#   b3    14    10    13     max(14-10, |14-12|, |10-12|) = 4
#   b4    15    13    14     max(15-13, |15-13|, |13-13|) = 2
#
#   period = 2
#   seed  = mean(TR1, TR2)        = (4 + 2) / 2   = 3
#   ATR3  = (3   * 1 + 4) / 2                     = 3.5
#   ATR4  = (3.5 * 1 + 2) / 2                     = 2.75
WORKED = [
    _bar(0, "10", "10", "10"),
    _bar(1, "12", "8", "11"),
    _bar(2, "13", "11", "12"),
    _bar(3, "14", "10", "13"),
    _bar(4, "15", "13", "14"),
]


def test_true_range_takes_the_widest_of_its_three_candidates() -> None:
    assert true_range(Decimal("12"), Decimal("8"), Decimal("10")) == Decimal("4")


def test_true_range_uses_the_gap_when_it_exceeds_the_bar() -> None:
    """A bar that opens far from the previous close has a true range wider
    than its own high-low, which is the whole reason the measure exists."""

    assert true_range(Decimal("21"), Decimal("20"), Decimal("10")) == Decimal("11")


def test_wilder_atr_matches_the_hand_worked_example() -> None:
    """Expected value derived from the definition above, not from this code."""

    assert wilder_atr(WORKED, period=2) == Decimal("2.75")


def test_wilder_atr_needs_one_more_bar_than_its_period() -> None:
    """The first true range needs a previous close, so a period of 2 needs
    three bars: two true ranges to seed from, and the bar before them."""

    with pytest.raises(InsufficientHistoryError):
        wilder_atr(WORKED[:2], period=2)

    assert wilder_atr(WORKED[:3], period=2) == Decimal("3")


def test_wilder_atr_rejects_a_non_positive_period() -> None:
    with pytest.raises(ValueError, match="period"):
        wilder_atr(WORKED, period=0)


def test_a_flat_series_has_zero_range() -> None:
    flat = [_bar(i, "10", "10", "10") for i in range(5)]

    assert wilder_atr(flat, period=2) == Decimal("0")
```

- [ ] **Step 3: Run them to verify they fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features -q --no-cov`

Expected: collection ERROR — the module does not exist.

- [ ] **Step 4: Implement volatility**

```python
# src/trading_house/features/indicators/volatility.py
"""True range and Wilder's ATR. Pure: bars in, a Decimal out.

These feed stop distance in the master specification's section 8.2, which
feeds lot size in 8.1. A wrong number here is a wrong stop and a wrong
position on a live order, so the correctness bar is the execution path's.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

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
        for previous, current in zip(bars, bars[1:], strict=False)
    ]
    atr = sum(ranges[:period], start=Decimal(0)) / period
    for value in ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr
```

- [ ] **Step 5: Write the failing spread tests**

```python
# tests/unit/features/test_spread.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.indicators.spread import median_spread_points
from trading_house.marketdata.models import Bar, BarQuality, Timeframe

BASE = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _bar(minute: int, spread: int) -> Bar:
    opened = BASE + timedelta(minutes=minute)
    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        event_time=opened,
        availability_time=opened + timedelta(minutes=1),
        open=Decimal("1.1"),
        high=Decimal("1.2"),
        low=Decimal("1.0"),
        close=Decimal("1.15"),
        tick_volume=10,
        spread=spread,
        real_volume=0,
        quality=BarQuality.OK,
    )


def test_an_odd_window_takes_the_middle_value() -> None:
    assert median_spread_points([_bar(0, 5), _bar(1, 9), _bar(2, 7)]) == Decimal("7")


def test_an_even_window_averages_the_two_middle_values() -> None:
    """Which is why this returns Decimal rather than the int the store holds."""

    assert median_spread_points([_bar(0, 5), _bar(1, 8), _bar(2, 6), _bar(3, 9)]) == Decimal("7")


def test_one_spike_does_not_move_the_median() -> None:
    """The reason for median over mean or latest: a single wide print would
    otherwise widen every stop derived from it."""

    calm = [_bar(i, 6) for i in range(9)]
    spiked = [*calm, _bar(9, 900)]

    assert median_spread_points(spiked) == Decimal("6")


def test_an_empty_window_is_a_refusal_not_a_zero() -> None:
    """A zero spread would read as a free market and shrink the cost term of
    every stop computed from it."""

    with pytest.raises(InsufficientHistoryError):
        median_spread_points([])
```

- [ ] **Step 6: Implement spread**

```python
# src/trading_house/features/indicators/spread.py
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
```

- [ ] **Step 7: Run the tests and the full suite**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features -q --no-cov
UV_SYSTEM_CERTS=1 uv run pytest -q
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
```

Expected: features tests pass; full suite at or above the 840/3 baseline; gates exit 0.

If `statistics.median` will not accept a generator under mypy strict, pass it a `list` instead — do not add a `# type: ignore`.

- [ ] **Step 8: Commit**

```bash
git add src/trading_house/features src/trading_house/core/errors.py tests/unit/features
git commit -m "feat: true range, wilder atr and median spread as pure functions"
```

---

### Task 2: The Engine and Its Fixed Window

**Files:**
- Create: `src/trading_house/features/engine.py`
- Test: `tests/unit/features/test_engine.py`

**Interfaces:**
- Consumes: `wilder_atr`, `median_spread_points`, `InsufficientHistoryError` (Task 1); `BarStore`, `Coverage`, `duration` from `marketdata`
- Produces: `FeatureEngine(store)` with `atr(instrument_id, timeframe, *, period, as_of) -> Decimal` and `median_spread_points(instrument_id, timeframe, *, window, as_of) -> Decimal`; `WARMUP_MULTIPLE`, `SPAN_SAFETY`

**The problem this task solves, stated plainly.** The store's read is *range*-based; the fixed lookback is *count*-based. Gaps mean N bars do not span N periods of calendar time — a week of M1 is 7,200 bars, not 10,080, because the weekend is closed. So the engine asks for a generously wide range, then takes the last N bars from it. Widening is safe; the slice is what makes the answer deterministic.

Two traps in that: `bars()` raises `CoverageError` when `start` precedes the earliest stored bar, so a generous range would raise on a store with short history even when N bars exist — the engine must clamp `start` to coverage first. And a store holding more history must still give the same answer, which is exactly what the slice guarantees.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/features/test_engine.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_house.core.errors import CoverageError, InsufficientHistoryError
from trading_house.features.engine import WARMUP_MULTIPLE, FeatureEngine
from trading_house.marketdata.models import Bar, BarQuality, Coverage, Timeframe

BASE = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _bar(minute: int, *, high: str = "1.2", low: str = "1.0", spread: int = 9) -> Bar:
    opened = BASE + timedelta(minutes=minute)
    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        event_time=opened,
        availability_time=opened + timedelta(minutes=1),
        open=Decimal("1.1"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("1.15"),
        tick_volume=10,
        spread=spread,
        real_volume=0,
        quality=BarQuality.OK,
    )


class FakeStore:
    """A BarStore holding a contiguous run of M1 bars, no database."""

    def __init__(self, count: int) -> None:
        self.all = [_bar(i) for i in range(count)]
        self.requests: list[tuple[datetime, datetime, datetime]] = []

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
    return FeatureEngine(store), store  # type: ignore[arg-type]


LATER = BASE + timedelta(days=30)


def test_the_same_instant_gives_the_same_atr_however_much_history_is_stored() -> None:
    """THE test of this phase (I-18).

    Wilder's ATR is recursive, so an implementation that computed over
    'whatever the store holds' would return a different number as history
    accumulated -- a backtest re-run months later would size positions
    differently with no code change and nothing to point at.
    """

    shallow, _ = _engine(200)
    deep, _ = _engine(2000)
    args = {"period": 14, "as_of": LATER}

    assert shallow.atr("fx.eurusd", Timeframe.M1, **args) == deep.atr(
        "fx.eurusd", Timeframe.M1, **args
    )


def test_the_window_is_exactly_the_period_times_the_multiple() -> None:
    """Fixed, not 'enough'. The multiple is what makes the seed's influence
    negligible while keeping the window a constant rather than a judgement."""

    engine, store = _engine(2000)

    engine.atr("fx.eurusd", Timeframe.M1, period=14, as_of=LATER)

    start, end, as_of = store.requests[-1]
    assert end == as_of == LATER
    assert (end - start) >= timedelta(minutes=14 * WARMUP_MULTIPLE)


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
    engine = FeatureEngine(store)  # type: ignore[arg-type]

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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features/test_engine.py -q --no-cov`

Expected: collection ERROR — `features.engine` does not exist.

- [ ] **Step 3: Implement the engine**

```python
# src/trading_house/features/engine.py
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

from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.indicators.spread import median_spread_points as _median_spread
from trading_house.features.indicators.volatility import wilder_atr
from trading_house.marketdata.models import Bar, Timeframe, duration
from trading_house.marketdata.store import BarStore

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


class FeatureEngine:
    """Computes features from stored bars over a deterministic window."""

    def __init__(self, store: BarStore) -> None:
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

        bars = self._window(instrument_id, timeframe, count=period * WARMUP_MULTIPLE + 1, as_of=as_of)
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
        wide and slices. The clamp to coverage matters: ``bars()`` raises
        ``CoverageError`` when the requested start precedes the earliest
        stored bar, which a generous range would trigger on a young store
        that nonetheless holds enough bars.
        """

        coverage = self._store.coverage(instrument_id, timeframe)
        if coverage.earliest_event_time is None:
            raise InsufficientHistoryError

        span = duration(timeframe) * count * SPAN_SAFETY
        start = max(as_of - span, coverage.earliest_event_time)
        bars = self._store.bars(
            instrument_id, timeframe, start=start, end=as_of, as_of=as_of
        )
        if len(bars) < count:
            raise InsufficientHistoryError
        return bars[-count:]
```

- [ ] **Step 4: Run the tests**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features -q --no-cov`

Expected: PASS.

If `test_the_same_instant_gives_the_same_atr_however_much_history_is_stored` fails, the slice is wrong — the engine is computing over more than `count` bars. That test is the phase's whole point; do not weaken it to pass.

- [ ] **Step 5: Prove the determinism test discriminates**

Temporarily change `_window`'s final line from `return bars[-count:]` to `return bars`, run the engine tests, and confirm the determinism test fails. Restore it. Report what the failure said.

A test that cannot fail is worse than no test, and this project has shipped one before.

- [ ] **Step 6: Full suite and gates, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest -q
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/features tests/unit/features
git commit -m "feat: compute features over a fixed window so the answer is reproducible"
```

---

### Task 3: Property Tests, the Architecture Guard, and Documentation

**Files:**
- Create: `tests/property/test_features.py`, `tests/acceptance/test_phase2.py`
- Modify: `tests/acceptance/test_architecture.py`, `README.md`, `mt5-multi-agent-trading-house-spec.md`

- [ ] **Step 1: Property tests**

```python
# tests/property/test_features.py
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from trading_house.features.indicators.volatility import true_range, wilder_atr

PRICES = st.decimals(min_value=Decimal("0.1"), max_value=Decimal("1000"), places=5)


@given(high=PRICES, low=PRICES, previous_close=PRICES)
def test_true_range_is_never_negative(high: Decimal, low: Decimal, previous_close: Decimal) -> None:
    """It is a distance. A negative one would shrink a stop toward zero."""

    assert true_range(high, low, previous_close) >= 0


@given(high=PRICES, low=PRICES, previous_close=PRICES)
def test_true_range_is_at_least_the_bar_s_own_range(
    high: Decimal, low: Decimal, previous_close: Decimal
) -> None:
    """The gap terms can only widen it, never narrow it."""

    assert true_range(high, low, previous_close) >= high - low
```

Then the property over `wilder_atr` itself. Generate *coherent* bars by
construction rather than filtering — `Bar` validates OHLC ordering for a clean
bar, so unconstrained generation spends the whole budget on rejected examples:

```python
@st.composite
def _coherent_bars(draw: st.DrawFn, count: int) -> list[Bar]:
    bars = []
    for index in range(count):
        low = draw(PRICES)
        span = draw(st.decimals(min_value=Decimal("0"), max_value=Decimal("50"), places=5))
        high = low + span
        mid = low + span / 2
        opened = BASE + timedelta(minutes=index)
        bars.append(
            Bar(
                instrument_id="fx.eurusd",
                timeframe=Timeframe.M1,
                event_time=opened,
                availability_time=opened + timedelta(minutes=1),
                open=mid,
                high=high,
                low=low,
                close=mid,
                tick_volume=1,
                spread=1,
                real_volume=0,
                quality=BarQuality.OK,
            )
        )
    return bars


@given(bars=_coherent_bars(count=20))
def test_atr_never_exceeds_the_widest_true_range_it_saw(bars: list[Bar]) -> None:
    """An average cannot exceed its own maximum. If it does, the smoothing
    recursion is wrong in a way that would widen every stop built on it."""

    widest = max(
        true_range(current.high, current.low, previous.close)
        for previous, current in zip(bars, bars[1:], strict=False)
    )
    result = wilder_atr(bars, period=5)

    assert 0 <= result <= widest
```

**Then verify the property is reachable**: print how many of 500 generated examples produced a valid `Bar` list. If it is zero, the strategy never exercises the assertion and the test is decoration. This project shipped exactly that defect in Phase 1.5 — a property whose pass branch was never reached in 2,000 examples.

- [ ] **Step 2: Architecture guard**

In `tests/acceptance/test_architecture.py`, add a test asserting that no module under `src/trading_house/features/` imports from `trading_house.brokers` or `trading_house.risk`, **plus a guard-the-guard test** proving the check fires. The file already carries this pair for `marketdata/` and for `terminal.py` — follow that shape exactly. A guard nobody has watched fail is not a guard.

- [ ] **Step 3: Phase 2 acceptance**

```python
# tests/acceptance/test_phase2.py
"""Phase 2 acceptance: features are reproducible and cannot bypass the store.

The general import rule lives in ``test_architecture.py``; this file asserts
what Phase 2 itself promised.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FEATURES = PROJECT_ROOT / "src" / "trading_house" / "features"
INDICATORS = FEATURES / "indicators"


def _module_functions(path: Path) -> list[ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node for node in tree.body if isinstance(node, ast.FunctionDef)]


def test_every_indicator_returns_decimal() -> None:
    """Features flow into stop distance and then into lot size. A float here
    would put float back on the money path Phase 0 spent effort removing."""

    checked = 0
    for path in sorted(INDICATORS.glob("*.py")):
        for function in _module_functions(path):
            if function.name.startswith("_"):
                continue
            assert function.returns is not None, f"{path.name}:{function.name} unannotated"
            assert ast.unparse(function.returns) == "Decimal", (
                f"{path.name}:{function.name} returns {ast.unparse(function.returns)}"
            )
            checked += 1
    assert checked >= 3, "expected true_range, wilder_atr and median_spread_points"


def test_no_indicator_can_reach_the_store() -> None:
    """Only the engine holds the store. If an indicator could fetch, a caller
    could obtain a feature without a point-in-time read and without the fixed
    window, which is exactly what I-18 forbids."""

    for path in sorted(INDICATORS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "BarStore" not in source, f"{path.name} reaches the store"
        assert "marketdata.store" not in source, f"{path.name} imports the store module"


def test_the_engine_holds_the_store() -> None:
    """The guard above is only meaningful while something still does."""

    assert "BarStore" in (FEATURES / "engine.py").read_text(encoding="utf-8")


def test_features_reaches_marketdata_and_nothing_else() -> None:
    """Phase-level restatement of the rule test_architecture.py owns."""

    forbidden = ("trading_house.brokers", "trading_house.risk", "trading_house.execution")
    for path in sorted(FEATURES.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in source, f"{path.name} imports {name}"
```

`test_the_engine_holds_the_store` is there deliberately: without it, deleting
`engine.py` entirely would leave `test_no_indicator_can_reach_the_store`
passing vacuously.

- [ ] **Step 4: Documentation**

- `README.md` — a short "Phase 2 — features" section: what the engine computes, that ATR uses a fixed ten-period-multiple lookback and why, and that insufficient history raises rather than returning a number.
- `mt5-multi-agent-trading-house-spec.md` — add **I-18** to the invariant table: *A feature is computed over a fixed lookback, so the same instrument, timeframe, period and `as_of` always yield the same value.* Enforcement column: `tests/unit/features/test_engine.py`.

- [ ] **Step 5: Full verification gate**

```bash
UV_SYSTEM_CERTS=1 uv lock --check
UV_SYSTEM_CERTS=1 uv run ruff format --check . && UV_SYSTEM_CERTS=1 uv run ruff check .
UV_SYSTEM_CERTS=1 uv run mypy
UV_SYSTEM_CERTS=1 uv run pytest -q
git status --short
```

Every command exits 0; coverage at or above 95%; no stray artifacts.

- [ ] **Step 6: Commit**

```bash
git add tests README.md mt5-multi-agent-trading-house-spec.md
git commit -m "docs: accept phase 2 feature engine"
```

---

## Final Verification Gate

Run from the repository root without relying on earlier output:

```bash
UV_SYSTEM_CERTS=1 uv lock --check
UV_SYSTEM_CERTS=1 uv run ruff format --check .
UV_SYSTEM_CERTS=1 uv run ruff check .
UV_SYSTEM_CERTS=1 uv run mypy
UV_SYSTEM_CERTS=1 uv run pytest -q
git status --short
```

Expected: every command exits `0`; coverage at or above 95%; `features/` imports nothing from `brokers/` or `risk/`; the Phase 0, 0.5, 1 and 1.5 acceptance suites still green.

## Deliberately Not In This Plan

| Item | Where it lands |
|---|---|
| Every indicator not named in §8.2 — RSI, VWAP, moving averages, imbalance | When a strategy asks, through the pure-function contract |
| A materialised feature store | Later, if recompute-on-demand is measurably too slow |
| Incremental or streaming computation | The orchestration plane, if the hot loop needs it |
| Any feature over ticks | Phase 1.5 stores none |
| Realised volatility | Not consumed by §8.2 |
| Converting spread points to price | Phase 3, which holds the instrument contract |
