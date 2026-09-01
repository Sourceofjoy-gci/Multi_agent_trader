# Phase 1.5 — Market Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store closed MetaTrader 5 bars for FX and metals in PostgreSQL, gated for impossibility, readable only point-in-time, with every ingest run recorded so short data is loud instead of silent.

**Architecture:** Four pure modules (`models`, `quality`, `sessions`, `paging`) carry all the logic and need neither a database nor a broker. `ingest.py` orchestrates them against a one-method `HistoryProvider` protocol — implemented by the Phase 1 MT5 adapter, so `marketdata/` never imports MetaTrader5. `store.py` writes to two append-only tables and reads them back filtered on `availability_time`.

**Tech Stack:** Python 3.12, Pydantic v2 (strict/frozen/extra=forbid), `Decimal` for prices, psycopg 3, Alembic, pytest with Hypothesis, testcontainers for PostgreSQL, uv.

**Spec:** `docs/superpowers/specs/2026-09-01-phase-1-5-market-data-design.md`

## Global Constraints

- **Every `uv` command must be prefixed `UV_SYSTEM_CERTS=1`** or it fails TLS verification in this environment. The plan's commands omit it; add it.
- The mypy gate is `uv run mypy` with **no path arguments** — the project scopes it via `packages = ["trading_house"]` in `pyproject.toml`. Passing paths hits an unrelated pre-existing basename collision.
- **`src/trading_house/marketdata/` must never import MetaTrader5**, directly or transitively. Only `src/trading_house/brokers/mt5/terminal.py` may import it, enforced by `tests/acceptance/test_architecture.py`.
- **Phase 1 is still read-only.** No `order_send` may appear anywhere in `src/`; `tests/acceptance/test_phase1.py` fails on the literal string, comments included.
- Line length 100. mypy strict. **No new `# type: ignore` in `src/`.**
- All datetimes are timezone-aware UTC (I-10). All models are strict, frozen, `extra="forbid"`.
- Prices are `Decimal`, converted with `decimal_of()` from `brokers/mt5/contracts.py` — never `Decimal(float)`.
- Migrations run as `trading_house_owner` (`op.execute("SET ROLE trading_house_owner")` first), revoke from `PUBLIC`, and never run automatically at startup.
- `terminal.py` is capped at 80 statements by `tests/acceptance/test_architecture.py` and currently sits at 58.

## File Structure

| File | Responsibility |
|---|---|
| `marketdata/models.py` | `Timeframe`, `BarQuality`, `IngestOutcome`, `Bar`, `Coverage`, `IngestRun`; duration and alignment arithmetic |
| `marketdata/quality.py` | The four impossibility gates. Pure. |
| `marketdata/sessions.py` | FX/metal week model, expected-bar counting. Pure. |
| `marketdata/paging.py` | Window planning per timeframe. Pure. |
| `marketdata/provider.py` | `HistoryProvider` protocol — one method |
| `marketdata/ingest.py` | Backfill and update orchestration, run accounting |
| `marketdata/store.py` | PostgreSQL writer and point-in-time reader |
| `migrations/versions/0003_market_bars.py` | `marketdata` schema, two tables |
| `brokers/mt5/boundary.py` | `Mt5Bar` DTO, `TerminalPort.copy_rates_range` |
| `brokers/mt5/terminal.py` | The MT5 call |
| `brokers/mt5/adapter.py` | Implements `HistoryProvider` |
| `cli.py` | `data backfill`, `data update`, `data coverage` |

---

### Task 1: Timeframes, Models, and Alignment Arithmetic

**Files:**
- Create: `src/trading_house/marketdata/__init__.py`, `src/trading_house/marketdata/models.py`
- Test: `tests/unit/marketdata/test_models.py`

**Interfaces:**
- Produces: `Timeframe`, `BarQuality`, `IngestOutcome`, `Bar`, `Coverage`, `IngestRun`, `duration()`, `availability_of()`, `is_aligned()`
- Consumes: `CanonicalModel`, `InstrumentId` from `core.values`; `ensure_utc` from `core.clock`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/marketdata/test_models.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.marketdata.models import (
    Bar,
    BarQuality,
    Timeframe,
    availability_of,
    duration,
    is_aligned,
)


def _bar(**overrides: object) -> Bar:
    kwargs: dict[str, object] = {
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "event_time": datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        "availability_time": datetime(2026, 8, 25, 9, 1, tzinfo=UTC),
        "open": Decimal("1.10000"),
        "high": Decimal("1.10050"),
        "low": Decimal("1.09950"),
        "close": Decimal("1.10020"),
        "tick_volume": 42,
        "spread": 9,
        "real_volume": 0,
        "quality": BarQuality.OK,
    }
    kwargs.update(overrides)
    return Bar(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("timeframe", "seconds"),
    [
        (Timeframe.M1, 60),
        (Timeframe.M5, 300),
        (Timeframe.M15, 900),
        (Timeframe.H1, 3600),
        (Timeframe.H4, 14400),
        (Timeframe.D1, 86400),
    ],
)
def test_every_timeframe_knows_its_duration(timeframe: Timeframe, seconds: int) -> None:
    assert duration(timeframe) == timedelta(seconds=seconds)


def test_a_bar_is_available_only_once_it_has_closed() -> None:
    """The look-ahead guard. A 09:00 M1 bar is not knowable until 09:01."""

    opened = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)

    assert availability_of(Timeframe.M1, opened) == datetime(2026, 8, 25, 9, 1, tzinfo=UTC)
    assert availability_of(Timeframe.H4, opened) == datetime(2026, 8, 25, 13, 0, tzinfo=UTC)


def test_alignment_is_judged_in_the_brokers_frame_not_utc() -> None:
    """FBS runs UTC+3, so its H4 bars open at 21:00, 01:00, 05:00 UTC. A naive
    modulo against UTC would reject every one of them."""

    h4_open_utc = datetime(2026, 8, 25, 21, 0, tzinfo=UTC)

    assert is_aligned(Timeframe.H4, h4_open_utc, server_offset_seconds=10800)
    assert not is_aligned(Timeframe.H4, h4_open_utc, server_offset_seconds=0)


def test_a_misaligned_bar_is_detected_at_any_offset() -> None:
    stray = datetime(2026, 8, 25, 9, 0, 37, tzinfo=UTC)

    assert not is_aligned(Timeframe.M1, stray, server_offset_seconds=10800)


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValidationError):
        _bar(event_time=datetime(2026, 8, 25, 9, 0))


def test_a_clean_bar_must_have_positive_prices() -> None:
    """Defective bars are stored (D-2), so the model cannot demand gt=0
    outright -- but a bar claiming to be clean and carrying a zero price is
    incoherent, and that is worth refusing at construction."""

    with pytest.raises(ValidationError):
        _bar(low=Decimal("0"))


def test_a_defective_bar_may_carry_the_impossible_value_it_was_sent() -> None:
    bar = _bar(low=Decimal("0"), quality=BarQuality.NON_POSITIVE_PRICE)

    assert bar.low == Decimal("0")


def test_bars_are_frozen() -> None:
    with pytest.raises(ValidationError):
        _bar().open = Decimal("2")  # type: ignore[misc]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/marketdata/test_models.py -q --no-cov`

Expected: collection ERROR — the module does not exist.

- [ ] **Step 3: Implement the models**

```python
# src/trading_house/marketdata/models.py
"""What a stored bar is, and the arithmetic that decides when it was knowable.

``availability_time`` is the whole point of this module. A bar stamped 09:00
did not exist at 09:00; it existed at 09:01, when it closed. Everything that
reads market data filters on availability, never on the event time, and that
is what keeps a backtest from seeing its own future (I-17).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Self

from pydantic import NonNegativeInt, field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.values import CanonicalModel, InstrumentId


class Timeframe(str, Enum):  # noqa: UP042
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"


class BarQuality(str, Enum):  # noqa: UP042
    OK = "OK"
    OHLC_INCOHERENT = "OHLC_INCOHERENT"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    NEGATIVE_SPREAD = "NEGATIVE_SPREAD"
    MISALIGNED_TIMESTAMP = "MISALIGNED_TIMESTAMP"


class IngestOutcome(str, Enum):  # noqa: UP042
    COMPLETE = "COMPLETE"
    TRUNCATED = "TRUNCATED"
    SPARSE = "SPARSE"
    EMPTY = "EMPTY"
    FAILED = "FAILED"


_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.H1: 3_600,
    Timeframe.H4: 14_400,
    Timeframe.D1: 86_400,
}


def duration(timeframe: Timeframe) -> timedelta:
    return timedelta(seconds=_SECONDS[timeframe])


def availability_of(timeframe: Timeframe, event_time: datetime) -> datetime:
    """When a bar opening at ``event_time`` became knowable: when it closed."""

    return event_time + duration(timeframe)


def is_aligned(timeframe: Timeframe, event_time: datetime, *, server_offset_seconds: int) -> bool:
    """Whether a bar's open sits on a timeframe boundary in the BROKER's frame.

    Not in UTC. A broker running UTC+3 opens its H4 bars at 21:00, 01:00 and
    05:00 UTC, none of which divide 14400 -- a modulo against UTC would reject
    every H4 and D1 bar the broker ever sent. Adding the offset back recovers
    the server epoch, which is where the boundary actually is.
    """

    server_epoch = event_time.timestamp() + server_offset_seconds
    return int(server_epoch) % _SECONDS[timeframe] == 0


class Bar(CanonicalModel):
    """One closed bar, exactly as the broker reported it."""

    instrument_id: InstrumentId
    timeframe: Timeframe
    event_time: datetime
    availability_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: NonNegativeInt
    spread: int
    real_volume: NonNegativeInt
    quality: BarQuality

    @field_validator("event_time", "availability_time")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error

    @model_validator(mode="after")
    def a_clean_bar_is_coherent(self) -> Self:
        """Defective bars carry whatever the broker sent (D-2). A bar claiming
        to be clean while holding an impossible price is a different thing: a
        contradiction, and refusing it here stops a gate bug reaching storage.
        """

        if self.quality is BarQuality.OK and min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("a clean bar cannot carry a non-positive price")
        if self.availability_time <= self.event_time:
            raise ValueError("a bar is available only after it closes")
        return self


class Coverage(CanonicalModel):
    """What the store actually holds for one instrument and timeframe."""

    instrument_id: InstrumentId
    timeframe: Timeframe
    earliest_event_time: datetime | None
    latest_event_time: datetime | None
    latest_availability_time: datetime | None
    clean_bars: NonNegativeInt
    defective_bars: NonNegativeInt


class IngestRun(CanonicalModel):
    """One backfill or update attempt, and what it actually achieved.

    Exists so that a run which asked for a year and received a week leaves a
    record. Without it, "less data" and "the market was closed" look the same
    from the outside, forever.
    """

    run_id: UUID
    instrument_id: InstrumentId
    timeframe: Timeframe
    requested_from: datetime
    requested_to: datetime
    started_at: datetime
    finished_at: datetime
    earliest_event_time: datetime | None
    bars_returned: NonNegativeInt
    bars_stored: NonNegativeInt
    bars_rejected: NonNegativeInt
    bars_conflicting: NonNegativeInt
    expected_bars: NonNegativeInt
    coverage_ratio: NonNegativeDecimal
    outcome: IngestOutcome
    detail: str | None

    @field_validator(
        "requested_from", "requested_to", "started_at", "finished_at"
    )
    @classmethod
    def normalize_run_timestamps(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error
```

Add `from uuid import UUID` and `NonNegativeDecimal` to the imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/marketdata/test_models.py -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Run the gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: exit `0`. If `NonNegativeInt` is unavailable from `pydantic`, import it from `pydantic` directly — it is already used that way in `src/trading_house/core/venue.py`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/marketdata tests/unit/marketdata
git commit -m "feat: model bars and the availability arithmetic that guards them"
```

---

### Task 2: The Four Impossibility Gates

**Files:**
- Create: `src/trading_house/marketdata/quality.py`
- Test: `tests/unit/marketdata/test_quality.py`

**Interfaces:**
- Consumes: `Timeframe`, `BarQuality`, `is_aligned` (Task 1)
- Produces: `assess(open, high, low, close, spread, timeframe, event_time, server_offset_seconds) -> BarQuality`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/marketdata/test_quality.py
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_house.marketdata.models import BarQuality, Timeframe
from trading_house.marketdata.quality import assess

ALIGNED = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
OFFSET = 10800


def _assess(**overrides: object) -> BarQuality:
    kwargs: dict[str, object] = {
        "bar_open": Decimal("1.10000"),
        "high": Decimal("1.10050"),
        "low": Decimal("1.09950"),
        "close": Decimal("1.10020"),
        "spread": 9,
        "timeframe": Timeframe.M1,
        "event_time": ALIGNED,
        "server_offset_seconds": OFFSET,
    }
    kwargs.update(overrides)
    return assess(**kwargs)  # type: ignore[arg-type]


def test_a_normal_bar_passes() -> None:
    assert _assess() is BarQuality.OK


def test_a_high_below_the_body_is_impossible() -> None:
    assert _assess(high=Decimal("1.09000")) is BarQuality.OHLC_INCOHERENT


def test_a_low_above_the_body_is_impossible() -> None:
    assert _assess(low=Decimal("1.20000")) is BarQuality.OHLC_INCOHERENT


def test_a_flat_bar_is_coherent() -> None:
    """All four prices equal is a real, if quiet, minute -- not a defect."""

    flat = Decimal("1.10000")

    assert _assess(bar_open=flat, high=flat, low=flat, close=flat) is BarQuality.OK


@pytest.mark.parametrize("field", ["bar_open", "high", "low", "close"])
def test_a_non_positive_price_is_impossible(field: str) -> None:
    assert _assess(**{field: Decimal("0")}) is BarQuality.NON_POSITIVE_PRICE


def test_a_negative_spread_is_impossible() -> None:
    assert _assess(spread=-1) is BarQuality.NEGATIVE_SPREAD


def test_a_zero_spread_is_allowed() -> None:
    assert _assess(spread=0) is BarQuality.OK


def test_a_bar_off_its_timeframe_boundary_is_a_conversion_bug() -> None:
    """This gate is the tripwire for a wrong server-clock offset."""

    assert (
        _assess(event_time=datetime(2026, 8, 25, 9, 0, 37, tzinfo=UTC))
        is BarQuality.MISALIGNED_TIMESTAMP
    )


def test_price_defects_are_reported_before_alignment() -> None:
    """A bar with two defects reports one verdict. Ordering must be fixed, or
    the same bar classifies differently depending on evaluation order."""

    verdict = _assess(
        low=Decimal("0"), event_time=datetime(2026, 8, 25, 9, 0, 37, tzinfo=UTC)
    )

    assert verdict is BarQuality.NON_POSITIVE_PRICE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/marketdata/test_quality.py -q --no-cov`

Expected: collection ERROR.

- [ ] **Step 3: Implement the gates**

```python
# src/trading_house/marketdata/quality.py
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

from datetime import datetime
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
    server_offset_seconds: int,
) -> BarQuality:
    """Classify one bar. The first defect found wins, in a fixed order."""

    if min(bar_open, high, low, close) <= 0:
        return BarQuality.NON_POSITIVE_PRICE
    if not (low <= min(bar_open, close) and max(bar_open, close) <= high):
        return BarQuality.OHLC_INCOHERENT
    if spread < 0:
        return BarQuality.NEGATIVE_SPREAD
    if not is_aligned(timeframe, event_time, server_offset_seconds=server_offset_seconds):
        return BarQuality.MISALIGNED_TIMESTAMP
    return BarQuality.OK
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/marketdata/test_quality.py -q --no-cov`

Expected: PASS, 10 tests.

- [ ] **Step 5: Run the gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/trading_house/marketdata/quality.py tests/unit/marketdata/test_quality.py
git commit -m "feat: gate bars on impossibility, never on implausibility"
```

---

### Task 3: The FX Session Model and Expected-Bar Counting

**Files:**
- Create: `src/trading_house/marketdata/sessions.py`
- Test: `tests/unit/marketdata/test_sessions.py`

**Interfaces:**
- Consumes: `Timeframe`, `duration` (Task 1)
- Produces: `WEEK_OPEN_UTC_HOUR`, `WEEK_CLOSE_UTC_HOUR`, `is_liquid(instant)`, `expected_bars(timeframe, start, end)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/marketdata/test_sessions.py
from datetime import UTC, datetime, timedelta

from trading_house.marketdata.models import Timeframe
from trading_house.marketdata.sessions import expected_bars, is_liquid


def test_midweek_is_liquid() -> None:
    assert is_liquid(datetime(2026, 8, 26, 12, 0, tzinfo=UTC))  # a Wednesday


def test_saturday_is_not_liquid() -> None:
    assert not is_liquid(datetime(2026, 8, 29, 12, 0, tzinfo=UTC))


def test_the_week_closes_friday_evening_and_reopens_sunday_evening() -> None:
    """Measured on FBS: ~48h gaps ending Friday ~21:00 UTC."""

    assert is_liquid(datetime(2026, 8, 28, 20, 0, tzinfo=UTC))  # Friday 20:00
    assert not is_liquid(datetime(2026, 8, 28, 22, 0, tzinfo=UTC))  # Friday 22:00
    assert not is_liquid(datetime(2026, 8, 30, 20, 0, tzinfo=UTC))  # Sunday 20:00
    assert is_liquid(datetime(2026, 8, 30, 22, 0, tzinfo=UTC))  # Sunday 22:00


def test_a_full_liquid_day_expects_one_bar_per_minute() -> None:
    start = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)

    assert expected_bars(Timeframe.M1, start, start + timedelta(days=1)) == 1440


def test_a_weekend_expects_nothing() -> None:
    start = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)  # Saturday

    assert expected_bars(Timeframe.M1, start, start + timedelta(days=1)) == 0


def test_a_full_week_excludes_the_weekend() -> None:
    """The property that makes coverage_ratio meaningful: a seven-day window
    must not expect seven days of bars, or every honest run reads as sparse."""

    start = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)  # Monday
    week = expected_bars(Timeframe.M1, start, start + timedelta(days=7))

    assert 5 * 1440 <= week <= 6 * 1440


def test_higher_timeframes_scale_down() -> None:
    start = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
    day = timedelta(days=1)

    assert expected_bars(Timeframe.H1, start, start + day) == 24
    assert expected_bars(Timeframe.H4, start, start + day) == 6


def test_an_inverted_range_expects_nothing_rather_than_a_negative_count() -> None:
    later = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    assert expected_bars(Timeframe.M1, later, later - timedelta(days=1)) == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/marketdata/test_sessions.py -q --no-cov`

Expected: collection ERROR.

- [ ] **Step 3: Implement the session model**

```python
# src/trading_house/marketdata/sessions.py
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

from datetime import datetime, timedelta

from trading_house.marketdata.models import Timeframe, duration

WEEK_CLOSE_WEEKDAY = 4  # Friday
WEEK_OPEN_WEEKDAY = 6  # Sunday
WEEK_BOUNDARY_UTC_HOUR = 21

_PROBE_STEP = timedelta(minutes=1)


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
    count = 0
    cursor = start
    while cursor < end:
        if is_liquid(cursor):
            count += 1
        cursor += step
    return count
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/marketdata/test_sessions.py -q --no-cov`

Expected: PASS, 8 tests.

- [ ] **Step 5: Guard the walk against pathological input**

`expected_bars` walks one step at a time. A twenty-year M1 range would be ten
million iterations. Add this test and make it pass:

```python
def test_a_range_far_larger_than_any_page_is_refused_rather_than_walked() -> None:
    """Ten years of M1 is five million steps. Pages are bounded by design
    (Task 4); a caller asking for more has a bug, and spinning is a worse
    answer than saying so."""

    import pytest

    start = datetime(2016, 1, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="range too large"):
        expected_bars(Timeframe.M1, start, datetime(2026, 1, 1, tzinfo=UTC))
```

Implementation: before the loop, compute `(end - start) / step` and raise
`ValueError("range too large to count bar by bar")` above `200_000` steps.

- [ ] **Step 6: Run the gates and commit**

```bash
uv run pytest tests/unit/marketdata -q --no-cov
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/trading_house/marketdata/sessions.py tests/unit/marketdata/test_sessions.py
git commit -m "feat: model the fx week so coverage ratios mean something"
```

---

### Task 4: Page Planning

**Files:**
- Create: `src/trading_house/marketdata/paging.py`
- Test: `tests/unit/marketdata/test_paging.py`

**Interfaces:**
- Consumes: `Timeframe`, `duration` (Task 1)
- Produces: `PAGE_BARS`, `plan_backward(timeframe, newest, oldest) -> tuple[tuple[datetime, datetime], ...]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/marketdata/test_paging.py
from datetime import UTC, datetime, timedelta

import pytest

from trading_house.marketdata.models import Timeframe, duration
from trading_house.marketdata.paging import PAGE_BARS, plan_backward


def test_a_short_range_is_one_page() -> None:
    newest = datetime(2026, 8, 25, tzinfo=UTC)
    pages = plan_backward(Timeframe.H1, newest=newest, oldest=newest - timedelta(days=1))

    assert len(pages) == 1
    assert pages[0] == (newest - timedelta(days=1), newest)


def test_pages_are_returned_newest_first() -> None:
    """Backfill walks backwards from what it already has."""

    newest = datetime(2026, 8, 25, tzinfo=UTC)
    pages = plan_backward(Timeframe.M1, newest=newest, oldest=newest - timedelta(days=90))

    assert pages[0][1] == newest
    assert all(pages[i][0] >= pages[i + 1][1] for i in range(len(pages) - 1))


def test_pages_tile_the_range_with_no_gap_and_no_overlap() -> None:
    """A hole here is a permanently missing week nobody notices."""

    newest = datetime(2026, 8, 25, tzinfo=UTC)
    oldest = newest - timedelta(days=90)
    pages = plan_backward(Timeframe.M1, newest=newest, oldest=oldest)

    assert pages[-1][0] == oldest
    for older, newer in zip(pages[1:], pages[:-1], strict=True):
        assert older[1] == newer[0]


def test_no_page_can_exceed_the_brokers_row_cap() -> None:
    """Measured: 50,000 rows succeeds, 100,000 fails with Invalid params."""

    newest = datetime(2026, 8, 25, tzinfo=UTC)
    pages = plan_backward(Timeframe.M1, newest=newest, oldest=newest - timedelta(days=365))

    for start, end in pages:
        assert (end - start) <= duration(Timeframe.M1) * PAGE_BARS
    assert PAGE_BARS <= 50_000


def test_an_empty_range_plans_nothing() -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)

    assert plan_backward(Timeframe.M1, newest=now, oldest=now) == ()


def test_an_inverted_range_is_a_caller_bug() -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)

    with pytest.raises(ValueError, match="oldest"):
        plan_backward(Timeframe.M1, newest=now, oldest=now + timedelta(days=1))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/marketdata/test_paging.py -q --no-cov`

Expected: collection ERROR.

- [ ] **Step 3: Implement page planning**

```python
# src/trading_house/marketdata/paging.py
"""Split a history request into windows the broker will actually answer.

MetaTrader 5 caps a request by the size of its result, not by how old the data
is: 50,000 bars comes back, 100,000 fails with ``Invalid params``, and a range
query spanning 2000 to now fails for M1 through H1 for the same reason while
succeeding for D1. So pages are sized in bars and converted to a window per
timeframe.

Pages run newest to oldest because backfill walks backwards from the oldest
bar already stored.
"""

from __future__ import annotations

from datetime import datetime

from trading_house.marketdata.models import Timeframe, duration

PAGE_BARS = 20_000
"""Well under the measured 50,000-row ceiling, leaving room for a broker whose
cap is lower than FBS's."""


def plan_backward(
    timeframe: Timeframe, *, newest: datetime, oldest: datetime
) -> tuple[tuple[datetime, datetime], ...]:
    """Tile ``[oldest, newest)`` into windows, newest first."""

    if oldest > newest:
        raise ValueError("oldest must not be after newest")
    span = duration(timeframe) * PAGE_BARS
    pages: list[tuple[datetime, datetime]] = []
    end = newest
    while end > oldest:
        start = max(oldest, end - span)
        pages.append((start, end))
        end = start
    return tuple(pages)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/marketdata/test_paging.py -q --no-cov`

Expected: PASS, 6 tests.

- [ ] **Step 5: Run the gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/trading_house/marketdata/paging.py tests/unit/marketdata/test_paging.py
git commit -m "feat: tile history requests into pages the broker will answer"
```

---

### Task 5: The MT5 Bar Boundary

**Files:**
- Modify: `src/trading_house/brokers/mt5/boundary.py`, `src/trading_house/brokers/mt5/terminal.py`
- Test: `tests/unit/brokers/mt5/test_boundary.py`, `tests/unit/brokers/mt5/conftest.py`

**Interfaces:**
- Produces: `Mt5Bar` dataclass; `TerminalPort.copy_rates_range(server_symbol, timeframe_minutes, start, end)`
- Consumes: `server_time_to_utc` (already in `boundary.py`)

**Note for the implementer:** `TerminalPort` currently has exactly ten methods
and `tests/unit/brokers/mt5/test_boundary.py` asserts that surface in
`EXPECTED_PORT_METHODS`. Adding the eleventh means updating that set. Every
double must gain the method too: `FakeTerminal` in
`tests/unit/brokers/mt5/conftest.py`, `_StubTerminal` in
`tests/unit/test_cli.py`, and `_StubTerminal` in `tests/acceptance/test_phase1.py`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/unit/brokers/mt5/test_boundary.py
def test_a_bar_dto_is_frozen_and_carries_no_metatrader_types() -> None:
    from dataclasses import FrozenInstanceError

    from trading_house.brokers.mt5.boundary import Mt5Bar

    bar = Mt5Bar(
        event_time=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        open=1.1,
        high=1.2,
        low=1.0,
        close=1.15,
        tick_volume=42,
        spread=9,
        real_volume=0,
    )

    assert bar.event_time.tzinfo is not None
    with pytest.raises(FrozenInstanceError):
        bar.open = 2.0  # type: ignore[misc]
```

Update `EXPECTED_PORT_METHODS` in the same file to include `"copy_rates_range"`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/brokers/mt5/test_boundary.py -q --no-cov`

Expected: FAIL — `Mt5Bar` does not exist and the port surface does not match.

- [ ] **Step 3: Add the DTO and widen the port**

In `src/trading_house/brokers/mt5/boundary.py`, after `Mt5Tick`:

```python
@dataclass(frozen=True, slots=True)
class Mt5Bar:
    """One closed bar, with its timestamp already converted to UTC."""

    event_time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int
    real_volume: int
```

And in `TerminalPort`, after `symbol_tick`:

```python
    def copy_rates_range(
        self, server_symbol: str, timeframe_minutes: int, start: datetime, end: datetime
    ) -> Sequence[Mt5Bar] | None: ...
```

`timeframe_minutes` rather than a `Timeframe`: the boundary stays free of
`marketdata` types, exactly as it stays free of MetaTrader5 ones. The adapter
translates.

- [ ] **Step 4: Implement it in the terminal**

In `src/trading_house/brokers/mt5/terminal.py`:

```python
    def copy_rates_range(
        self, server_symbol: str, timeframe_minutes: int, start: datetime, end: datetime
    ) -> Sequence[Mt5Bar] | None:
        mt5.symbol_select(server_symbol, True)
        rates = mt5.copy_rates_range(server_symbol, timeframe_minutes, start, end)
        if rates is None:
            return None
        return tuple(
            Mt5Bar(
                event_time=self._to_utc(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                tick_volume=int(row[5]),
                spread=int(row[6]),
                real_volume=int(row[7]),
            )
            for row in rates
        )
```

MetaTrader 5's timeframe constants are the minute count for everything below
D1 (`TIMEFRAME_M1 == 1`, `TIMEFRAME_H4 == 16388` is the exception). Do **not**
assume minutes map directly. Add this mapping to `boundary.py` and have the
adapter use it:

```python
MT5_TIMEFRAME_CODES: dict[int, int] = {
    1: 1,  # M1
    5: 5,  # M5
    15: 15,  # M15
    60: 16385,  # H1  = TIMEFRAME_H1
    240: 16388,  # H4 = TIMEFRAME_H4
    1440: 16408,  # D1 = TIMEFRAME_D1
}
```

Each entry carries the MT5 constant name in a comment for the same reason the
retcode table does: the value and any test asserting it would otherwise be
authored from the same guess.

- [ ] **Step 5: Update every TerminalPort double**

Add to `FakeTerminal` (`tests/unit/brokers/mt5/conftest.py`), and to both
`_StubTerminal` classes (`tests/unit/test_cli.py`, `tests/acceptance/test_phase1.py`):

```python
    def copy_rates_range(
        self, server_symbol: str, timeframe_minutes: int, start: object, end: object
    ) -> tuple[object, ...]:
        return ()
```

- [ ] **Step 6: Verify the statement cap and run everything**

```bash
uv run python -c "import ast,pathlib; print(sum(1 for n in ast.walk(ast.parse(pathlib.Path('src/trading_house/brokers/mt5/terminal.py').read_text(encoding='utf-8'))) if isinstance(n, ast.stmt)), '/ 80')"
uv run pytest tests/unit tests/property tests/acceptance -q --no-cov
uv run ruff format . && uv run ruff check . && uv run mypy
```

Expected: under 80 statements; all tests pass. If the cap is exceeded, move
conversion into `boundary.py` rather than raising the cap.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/brokers/mt5 tests/unit/brokers/mt5 tests/unit/test_cli.py tests/acceptance/test_phase1.py
git commit -m "feat: let the terminal fetch a range of closed bars"
```

---

### Task 6: The HistoryProvider Seam

**Files:**
- Create: `src/trading_house/marketdata/provider.py`
- Modify: `src/trading_house/brokers/mt5/adapter.py`
- Test: `tests/unit/marketdata/test_provider.py`, `tests/unit/brokers/mt5/test_adapter.py`

**Interfaces:**
- Produces: `HistoryProvider` protocol with `history(instrument_id, timeframe, start, end) -> Sequence[Mt5Bar]`
- Consumes: `Mt5Bar` (Task 5), `Timeframe` (Task 1)

**Design note on import direction:** `marketdata/provider.py` imports
`brokers.mt5.boundary`, and `brokers/mt5/adapter.py` imports
`marketdata.models` for `Timeframe`. That is a package-level cycle but not a
module-level one -- `boundary.py` imports nothing from `marketdata` and
`models.py` imports nothing from `brokers` -- so it resolves cleanly. It is
deliberate rather than accidental: the alternative is a translation layer whose
only job would be renaming. The constraint that actually matters stays
one-directional and unchanged: `marketdata/` must not reach MetaTrader5.

**Design note:** the provider returns `Mt5Bar` today because it is a plain,
venue-free dataclass — it names MT5 only in its type name, carries no MT5
types, and needs no translation layer that would exist solely to be renamed.
A second venue implements the same protocol.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/marketdata/test_provider.py
from trading_house.marketdata.provider import HistoryProvider


def test_the_provider_surface_is_one_method() -> None:
    """A wide data port is a leaky one. Everything else the gateway can do is
    a trading concern and belongs on BrokerAdapter."""

    assert {name for name in vars(HistoryProvider) if not name.startswith("_")} == {"history"}


def test_the_mt5_adapter_satisfies_it() -> None:
    from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter

    assert hasattr(Mt5BrokerAdapter, "history")
```

- [ ] **Step 2: Run to verify failure, then implement the protocol**

```python
# src/trading_house/marketdata/provider.py
"""The only thing market-data ingest asks of a venue.

One method. Ingest depends on this and nothing else, which is why
``marketdata/`` contains no MetaTrader5 import and an acceptance test can say
so. Bars are a data concern, so this stays off ``BrokerAdapter``'s
eight-method trading surface while keeping the venue-neutral promise true for
market data too.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from trading_house.brokers.mt5.boundary import Mt5Bar
from trading_house.marketdata.models import Timeframe


@runtime_checkable
class HistoryProvider(Protocol):
    def history(
        self, instrument_id: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Sequence[Mt5Bar]: ...
```

- [ ] **Step 3: Implement `history` on the adapter**

Add to `Mt5BrokerAdapter` in `src/trading_house/brokers/mt5/adapter.py`:

```python
    def history(
        self, instrument_id: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Sequence[Mt5Bar]:
        """Fetch closed bars at the lowest priority band.

        A multi-hour backfill must never delay a protection call, which is the
        entire reason the gateway's queue is prioritised.
        """

        server_symbol = self._server_symbol_for(instrument_id)
        code = MT5_TIMEFRAME_CODES[int(duration(timeframe).total_seconds() // 60)]
        bars = self._gateway.call(
            Priority.MARKET_DATA,
            lambda t: t.copy_rates_range(server_symbol, code, start, end),
        )
        return () if bars is None else tuple(bars)
```

Add a test in `tests/unit/brokers/mt5/test_adapter.py` asserting that an
unbound instrument raises `ConfigurationError` from `history`, and that a
terminal returning `None` yields an empty tuple rather than `None`.

- [ ] **Step 4: Run the gates and commit**

```bash
uv run pytest tests/unit -q --no-cov
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/trading_house/marketdata/provider.py src/trading_house/brokers/mt5/adapter.py tests
git commit -m "feat: give market data a one-method venue seam"
```

---

### Task 7: The Migration

**Files:**
- Create: `migrations/versions/0003_market_bars.py`
- Test: `tests/integration/marketdata/test_migration.py`

**Interfaces:**
- Produces: schema `marketdata` with tables `bars` and `ingest_runs`

- [ ] **Step 1: Write the migration**

Follow `migrations/versions/0002_memory_and_trials.py` exactly for role
handling and revocation.

```python
# migrations/versions/0003_market_bars.py
"""Create the append-only market-data tables (I-17)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_market_bars"
down_revision: str | None = "0002_memory_and_trials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("CREATE SCHEMA marketdata AUTHORIZATION trading_house_owner")
    op.execute("REVOKE ALL ON SCHEMA marketdata FROM PUBLIC")

    op.execute(
        """
        CREATE TABLE marketdata.ingest_runs (
            run_id UUID PRIMARY KEY,
            instrument_id TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            requested_from TIMESTAMPTZ NOT NULL,
            requested_to TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ NOT NULL,
            earliest_event_time TIMESTAMPTZ,
            bars_returned INTEGER NOT NULL,
            bars_stored INTEGER NOT NULL,
            bars_rejected INTEGER NOT NULL,
            bars_conflicting INTEGER NOT NULL,
            expected_bars INTEGER NOT NULL,
            coverage_ratio NUMERIC NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT,
            CONSTRAINT runs_counts_non_negative CHECK (
                bars_returned >= 0 AND bars_stored >= 0
                AND bars_rejected >= 0 AND bars_conflicting >= 0
            ),
            CONSTRAINT runs_range_ordered CHECK (requested_to >= requested_from)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE marketdata.bars (
            instrument_id TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            event_time TIMESTAMPTZ NOT NULL,
            availability_time TIMESTAMPTZ NOT NULL,
            open NUMERIC NOT NULL,
            high NUMERIC NOT NULL,
            low NUMERIC NOT NULL,
            close NUMERIC NOT NULL,
            tick_volume BIGINT NOT NULL,
            spread INTEGER NOT NULL,
            real_volume BIGINT NOT NULL,
            quality TEXT NOT NULL,
            ingest_run_id UUID NOT NULL REFERENCES marketdata.ingest_runs (run_id),
            PRIMARY KEY (instrument_id, timeframe, event_time),
            CONSTRAINT bars_available_after_close CHECK (availability_time > event_time),
            CONSTRAINT bars_clean_prices_positive CHECK (
                quality <> 'OK'
                OR (open > 0 AND high > 0 AND low > 0 AND close > 0)
            ),
            CONSTRAINT bars_volumes_non_negative CHECK (
                tick_volume >= 0 AND real_volume >= 0
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX bars_point_in_time
            ON marketdata.bars (instrument_id, timeframe, availability_time)
        """
    )

    op.execute("GRANT USAGE ON SCHEMA marketdata TO trading_house_runtime")
    op.execute(
        """
        GRANT SELECT, INSERT ON marketdata.bars, marketdata.ingest_runs
            TO trading_house_runtime
        """
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP SCHEMA marketdata CASCADE")
```

`GRANT SELECT, INSERT` and nothing else: the runtime role must be unable to
`UPDATE` or `DELETE` a bar, which is what makes append-only a property of the
database rather than of the code that happens to be written today.

- [ ] **Step 2: Write the integration test**

```python
# tests/integration/marketdata/test_migration.py
"""The append-only guarantee must be the database's, not the code's."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_the_runtime_role_cannot_update_or_delete_a_bar(database: object) -> None:
    """Follow the privilege assertions in tests/integration/database/ for the
    exact harness fixture and connection style used by this project."""
```

Model this file on the existing privilege tests under `tests/integration/` —
open a runtime-role connection, attempt an `UPDATE` and a `DELETE` against
`marketdata.bars`, and assert both raise `psycopg.errors.InsufficientPrivilege`.

- [ ] **Step 3: Apply and verify**

```bash
uv run alembic -x "url=postgresql+psycopg://trading_house_migrator:PASSWORD@127.0.0.1/trading_house" upgrade head
uv run pytest tests/integration/marketdata -q --no-cov
```

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/0003_market_bars.py tests/integration/marketdata
git commit -m "feat: create the append-only market-data tables"
```

---

### Task 8: The Store's Write Path

**Files:**
- Create: `src/trading_house/marketdata/store.py`
- Test: `tests/integration/marketdata/test_store_writes.py`

**Interfaces:**
- Produces: `PostgresBarStore(connection_factory)` with `record_run(run) -> None` and `append_bars(bars, run_id) -> WriteResult`
- Consumes: `ConnectionFactory` pattern from `src/trading_house/audit/repository.py`; `Bar` (Task 1)

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/marketdata/test_store_writes.py
import pytest

pytestmark = pytest.mark.integration


def test_appending_the_same_bar_twice_stores_it_once(bar_store: BarStore) -> None:
    """Page boundaries overlap by design, so a second identical write is
    normal traffic and must be silent -- not an error, and not a conflict."""

    first = seed(bar_store, [_bar(0)])
    second = seed(bar_store, [_bar(0)])

    assert first.stored == 1
    assert second.stored == 0
    assert second.duplicate == 1
    assert second.conflicting == 0


def test_a_refetch_that_disagrees_is_counted_not_applied(bar_store: BarStore) -> None:
    """Brokers revise history. The first observation stands, the disagreement
    is counted, and nothing is silently rewritten (spec 4.3). Overwriting
    would mean a backtest run today and the same backtest run next month
    could differ with no code change and no record of why."""

    seed(bar_store, [_bar(0)])
    revised = _bar(0).model_copy(update={"close": Decimal("9.99999")})

    result = seed(bar_store, [revised])

    assert result.conflicting == 1
    assert result.stored == 0
    held = bar_store.bars(
        "fx.eurusd",
        Timeframe.M1,
        start=NINE,
        end=NINE + timedelta(hours=1),
        as_of=NINE + timedelta(hours=1),
    )
    assert held[0].close == Decimal("1.10020")


def test_a_defective_bar_is_stored_alongside_clean_ones(bar_store: BarStore) -> None:
    """D-2: the forensic record is the point. A gate I decide was too strict
    next month can only be re-adjudicated against data that was kept."""

    result = seed(bar_store, [_bar(0), _bar(1, quality=BarQuality.OHLC_INCOHERENT)])

    assert result.stored == 2
    assert bar_store.coverage("fx.eurusd", Timeframe.M1).defective_bars == 1


def test_a_bar_cannot_be_written_without_a_run_to_blame(bar_store: BarStore) -> None:
    """The foreign key is the point: every bar is traceable to the run that
    fetched it, so a suspect window can be tied back to how it was obtained."""

    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        bar_store.append_bars([_bar(0)], run_id=uuid4())
```

Reuse `_bar`, `seed` and the `bar_store` fixture from Task 9's test module
by lifting them into `tests/integration/marketdata/conftest.py` — pytest runs
under `--import-mode=importlib`, so cross-importing between test modules by
name is unreliable, exactly as it was for `FakeTerminal` in Phase 1. `seed`
returns the `WriteResult` so these tests can assert on it.

- [ ] **Step 2: Implement the writer**

Key requirements, in the module docstring and the code:

- `append_bars` uses `INSERT ... ON CONFLICT (instrument_id, timeframe, event_time) DO NOTHING`, then re-reads the conflicting keys and compares OHLC to distinguish *identical* re-writes (normal, silent) from *disagreeing* ones (counted).
- Returns `WriteResult(stored: int, duplicate: int, conflicting: int)`.
- `record_run` inserts the `ingest_run` row **before** any bars reference it.
- Failures raise `DatabaseUnavailableError`; no DSN or row content in any message. Follow the `_AuditRepositoryFailure` redaction pattern in `audit/repository.py`.

- [ ] **Step 3: Run and commit**

```bash
uv run pytest tests/integration/marketdata -q --no-cov
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/trading_house/marketdata/store.py tests/integration/marketdata
git commit -m "feat: append bars without ever rewriting one"
```

---

### Task 9: The Store's Read Path

**Files:**
- Modify: `src/trading_house/marketdata/store.py`
- Test: `tests/integration/marketdata/test_store_reads.py`

**Interfaces:**
- Produces: `bars(instrument_id, timeframe, *, start, end, as_of, include_defective=False) -> tuple[Bar, ...]`, `coverage(instrument_id, timeframe) -> Coverage`
- Consumes: `Coverage`, `Bar` (Task 1); `CoverageError` (add to `core/errors.py`)

- [ ] **Step 1: Add the typed error**

In `src/trading_house/core/errors.py`:

```python
class CoverageError(TradingHouseError):
    """Raised when a market-data read asks for more than the store holds."""

    public_message = "requested market data exceeds stored coverage"
```

And `ExitCode.COVERAGE = 10`. Add the mapping in `cli.py`'s `EXIT_CODES` and
extend the exit-code test in `tests/unit/test_cli.py`.

- [ ] **Step 2: Write the failing tests — the point-in-time test first**

```python
# tests/integration/marketdata/test_store_reads.py
import pytest

pytestmark = pytest.mark.integration


from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading_house.core.errors import CoverageError
from trading_house.marketdata.models import Bar, BarQuality, Timeframe

NINE = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _bar(minute: int, *, quality: BarQuality = BarQuality.OK) -> Bar:
    opened = NINE + timedelta(minutes=minute)
    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        event_time=opened,
        availability_time=opened + timedelta(minutes=1),
        open=Decimal("1.10000"),
        high=Decimal("1.10050"),
        low=Decimal("1.09950"),
        close=Decimal("1.10020"),
        tick_volume=10,
        spread=9,
        real_volume=0,
        quality=quality,
    )


def test_a_bar_that_had_not_closed_yet_is_invisible(bar_store: BarStore) -> None:
    """THE test of this phase (I-17).

    Bars opening at 09:00, 09:01 and 09:02 become knowable at 09:01, 09:02
    and 09:03. Standing at 09:02 you may see the first two and must not see
    the third -- it has not closed. An implementation that filters on
    event_time instead of availability_time returns all three, and every
    backtest built on it is reading its own future.
    """

    seed(bar_store, [_bar(0), _bar(1), _bar(2)])

    visible = bar_store.bars(
        "fx.eurusd",
        Timeframe.M1,
        start=NINE,
        end=NINE + timedelta(hours=1),
        as_of=NINE + timedelta(minutes=2),
    )

    assert [b.event_time for b in visible] == [NINE, NINE + timedelta(minutes=1)]


def test_a_bar_becomes_visible_exactly_at_its_close(bar_store: BarStore) -> None:
    """The boundary is inclusive. Exclusive would make every bar invisible for
    one tick -- the kind of off-by-one that surfaces months later as an
    inexplicable one-bar lag in a strategy."""

    seed(bar_store, [_bar(0)])

    at_close = bar_store.bars(
        "fx.eurusd",
        Timeframe.M1,
        start=NINE,
        end=NINE + timedelta(hours=1),
        as_of=NINE + timedelta(minutes=1),
    )

    assert len(at_close) == 1


def test_defective_bars_are_absent_by_default_and_present_on_request(
    bar_store: BarStore,
) -> None:
    seed(bar_store, [_bar(0), _bar(1, quality=BarQuality.OHLC_INCOHERENT)])
    window = {
        "start": NINE,
        "end": NINE + timedelta(hours=1),
        "as_of": NINE + timedelta(hours=1),
    }

    assert len(bar_store.bars("fx.eurusd", Timeframe.M1, **window)) == 1
    assert len(bar_store.bars("fx.eurusd", Timeframe.M1, **window, include_defective=True)) == 2


def test_asking_beyond_coverage_raises_rather_than_returning_less(
    bar_store: BarStore,
) -> None:
    """Returning three months when five years were asked for, with a healthy
    exit code, is the exact failure this phase exists to prevent."""

    seed(bar_store, [_bar(0)])

    with pytest.raises(CoverageError):
        bar_store.bars(
            "fx.eurusd",
            Timeframe.M1,
            start=NINE - timedelta(days=365),
            end=NINE + timedelta(hours=1),
            as_of=NINE + timedelta(hours=1),
        )


def test_coverage_reports_what_is_actually_held(bar_store: BarStore) -> None:
    seed(bar_store, [_bar(0), _bar(1), _bar(2, quality=BarQuality.NEGATIVE_SPREAD)])

    coverage = bar_store.coverage("fx.eurusd", Timeframe.M1)

    assert coverage.earliest_event_time == NINE
    assert coverage.latest_event_time == NINE + timedelta(minutes=2)
    assert coverage.clean_bars == 2
    assert coverage.defective_bars == 1


def test_coverage_of_an_empty_key_is_not_an_error(bar_store: BarStore) -> None:
    """Starting from nothing is an ordinary state, and distinct from a short
    read. A caller checking coverage before its first backfill must not get an
    exception for it."""

    coverage = bar_store.coverage("metal.xauusd", Timeframe.H1)

    assert coverage.earliest_event_time is None
    assert coverage.clean_bars == 0
```

Write two helpers in the same file: a `bar_store` fixture yielding a
`PostgresBarStore` against the integration harness, and `seed(store, bars)`
which records a minimal valid `IngestRun` and appends the bars against its
`run_id`. Follow `tests/integration/ops/test_health.py` for the harness fixture
and connection style.

- [ ] **Step 3: Implement the reader**

- Every query filters `availability_time <= %(as_of)s`. There is no code path
  that does not. `as_of` is a required keyword argument with no default (D-8).
- `include_defective=False` adds `AND quality = 'OK'`.
- Before returning, compare the requested `[start, end)` against `coverage()`.
  If `start` precedes `earliest_event_time`, raise `CoverageError`.
- `coverage()` is a single aggregate query over the primary-key index.

- [ ] **Step 4: Run and commit**

```bash
uv run pytest tests/integration/marketdata tests/unit -q --no-cov
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/trading_house/marketdata/store.py src/trading_house/core/errors.py src/trading_house/cli.py tests
git commit -m "feat: read market data point-in-time or not at all"
```

---

### Task 10: Ingest Orchestration

**Files:**
- Create: `src/trading_house/marketdata/ingest.py`
- Test: `tests/unit/marketdata/test_ingest.py`

**Interfaces:**
- Produces: `backfill(provider, store, clock, *, instrument_id, timeframe, until, server_offset_seconds) -> IngestRun`, `update(...) -> IngestRun`
- Consumes: everything from Tasks 1-4, 6, 8, 9

- [ ] **Step 1: Write the failing tests against fakes**

A `FakeProvider` returning canned pages and a `FakeStore` recording calls —
no database, no broker. Write `FakeStore` with a `coverage()` returning a
configurable `Coverage`, an `appended` list, and no-op `record_run`. Define
`_mt5_bar(minute, **overrides)` building an `Mt5Bar`, `_full_liquid_page()`
returning one bar per minute across a liquid hour, and the `BACKFILL_ARGS` /
`ONE_HOUR_BACKFILL` / `UPDATE_ARGS` keyword bundles (`instrument_id`,
`timeframe`, `until`, `server_offset_seconds`). `FIXED_CLOCK` is a
`FixedClock` from `core.clock`.

```python
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


def test_an_empty_page_is_retried_once_before_being_believed() -> None:
    """MT5 downloads history lazily, so a first deep request genuinely can
    return less than a second one. Concluding exhaustion on a single empty
    answer would leave real history permanently unfetched."""

    provider = FakeProvider([[], [_mt5_bar(0)]])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, **BACKFILL_ARGS)

    assert len(provider.requests) >= 2
    assert run.bars_returned == 1


def test_the_single_bar_artifact_counts_as_exhaustion_not_as_data() -> None:
    """Measured on FBS: asking for six-month-old M1 returns exactly one bar
    and no error. Storing that and reporting success is the failure this
    entire phase exists to prevent."""

    provider = FakeProvider([[_mt5_bar(0)], [_mt5_bar(0)]])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, **BACKFILL_ARGS)

    assert run.outcome in {IngestOutcome.TRUNCATED, IngestOutcome.EMPTY}


def test_hitting_the_depth_wall_is_truncated_not_complete() -> None:
    """TRUNCATED and COMPLETE differ by whether the requested start was
    reached. Collapsing them would make a backtest unable to tell a full
    history from a wall."""

    provider = FakeProvider([[_mt5_bar(i) for i in range(50)], [], []])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, **BACKFILL_ARGS)

    assert run.outcome is IngestOutcome.TRUNCATED
    assert run.earliest_event_time is not None


def test_reaching_the_requested_start_with_full_coverage_is_complete() -> None:
    provider = FakeProvider([_full_liquid_page()])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, **ONE_HOUR_BACKFILL)

    assert run.outcome is IngestOutcome.COMPLETE
    assert run.coverage_ratio >= Decimal("0.5")


def test_reaching_the_start_with_thin_data_is_sparse_not_complete() -> None:
    """The distinction that stops a half-empty window passing as success."""

    provider = FakeProvider([_full_liquid_page()[:5]])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, **ONE_HOUR_BACKFILL)

    assert run.outcome is IngestOutcome.SPARSE


def test_the_run_records_how_far_back_it_actually_reached() -> None:
    """earliest_event_time is the fact a backtest needs and can obtain
    nowhere else."""

    bars = [_mt5_bar(i) for i in range(10)]
    provider = FakeProvider([bars, []])

    run = backfill(provider, FakeStore(), FIXED_CLOCK, **BACKFILL_ARGS)

    assert run.earliest_event_time == min(b.event_time for b in bars)


def test_defective_bars_are_counted_and_still_stored() -> None:
    """Rejected by the gates, kept by the store (D-2)."""

    store = FakeStore()
    provider = FakeProvider([[_mt5_bar(0), _mt5_bar(1, high=0.5)]])

    run = backfill(provider, store, FIXED_CLOCK, **ONE_HOUR_BACKFILL)

    assert run.bars_rejected == 1
    assert len(store.appended) == 2


def test_update_starts_from_the_newest_stored_bar() -> None:
    """Not from the beginning of time. The stored data is the cursor."""

    store = FakeStore(latest_event_time=NINE)
    provider = FakeProvider([[]])

    update(provider, store, FIXED_CLOCK, **UPDATE_ARGS)

    assert provider.requests[0][0] >= NINE


def test_the_forming_bar_is_never_requested() -> None:
    """Bar 0 is incomplete. Bounding the request at the last completed
    boundary is what stops a partial bar being stored as a closed one, and
    silently look-ahead-leaking into every backtest that reads it."""

    provider = FakeProvider([[]])
    now = datetime(2026, 8, 25, 9, 30, 45, tzinfo=UTC)

    update(provider, FakeStore(), FixedClock(now), **UPDATE_ARGS)

    _, end = provider.requests[0]
    assert end <= datetime(2026, 8, 25, 9, 30, tzinfo=UTC)
```

- [ ] **Step 2: Implement**

`backfill` walks `plan_backward` pages from `store.coverage().earliest_event_time`
(or `now`) toward `until`; `update` walks forward from `latest_event_time`. Both:

1. Truncate the newest bound to the last completed timeframe boundary.
2. For each page, call `provider.history`, retry once after a pause on an
   empty or single-bar result, and stop on the second failure.
3. Run `quality.assess` on every returned bar; build `Bar` models.
4. Call `store.record_run` first, then `store.append_bars`.
5. Compute `expected_bars` via `sessions.expected_bars` and the ratio.
6. Derive `outcome` per the spec's table.

The retry pause is injected as a callable so tests need no real sleep.

- [ ] **Step 3: Run and commit**

```bash
uv run pytest tests/unit/marketdata -q --no-cov
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/trading_house/marketdata/ingest.py tests/unit/marketdata/test_ingest.py
git commit -m "feat: make a short backfill a recorded fact rather than an absence"
```

---

### Task 11: The CLI

**Files:**
- Modify: `src/trading_house/cli.py`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `data backfill`, `data update`, `data coverage`

- [ ] **Step 1: Write the failing tests**

```python
def test_backfill_requires_an_explicit_instrument_and_timeframe() -> None:
    """A backfill is a deliberate, long-running act. Defaulting it invites
    one nobody meant to start."""


def test_update_iterates_the_signed_binding() -> None:


def test_coverage_renders_deterministic_json() -> None:


def test_data_commands_never_print_a_dsn() -> None:
```

- [ ] **Step 2: Implement**

Add `data_app = typer.Typer(...)` and `app.add_typer(data_app, name="data")`,
matching the existing `constitution_app` / `db_app` / `audit_app` pattern at
`src/trading_house/cli.py:84-90`. Reuse `_run` for error mapping and JSON
rendering. Build the provider through `_book_reconciler`'s existing lazy
terminal factory so MetaTrader5 is still imported inside a function only.

`data update` iterates every instrument in the signed binding across all six
timeframes; `data backfill` takes `--instrument`, `--timeframe`, `--from`.

- [ ] **Step 3: Run and commit**

```bash
uv run pytest tests/unit tests/integration -q --no-cov
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/trading_house/cli.py tests/unit/test_cli.py
git commit -m "feat: expose backfill, update and coverage as operator commands"
```

---

### Task 12: Property Tests, Acceptance, Live, and Documentation

**Files:**
- Create: `tests/property/test_marketdata.py`, `tests/acceptance/test_phase1_5.py`, `tests/live/test_mt5_bars.py`
- Modify: `tests/acceptance/test_architecture.py`, `README.md`, `mt5-multi-agent-trading-house-spec.md`

- [ ] **Step 1: Property tests**

```python
# tests/property/test_marketdata.py
from hypothesis import given
from hypothesis import strategies as st


@given(...)
def test_availability_always_follows_the_event(...) -> None:
    """No timeframe, no instant, no offset makes a bar knowable before it closed."""


@given(...)
def test_pages_always_tile_their_range(...) -> None:
    """No overlap, no hole, for any timeframe and any range. A hole here is a
    permanently missing window nobody notices."""


@given(...)
def test_a_bar_passing_the_gates_always_satisfies_ohlc_ordering(...) -> None:
```

- [ ] **Step 2: Architecture guard**

In `tests/acceptance/test_architecture.py`, add a test asserting no module
under `src/trading_house/marketdata/` imports `MetaTrader5`, plus a
guard-the-guard proving the check can still fail — follow
`test_metatrader5_is_importable_from_exactly_one_module` and its companion
`test_the_terminal_module_is_where_metatrader5_actually_lives`.

- [ ] **Step 3: Phase 1.5 acceptance**

```python
# tests/acceptance/test_phase1_5.py
def test_no_read_path_can_skip_the_availability_filter() -> None:
    """I-17. Parse store.py and assert every SELECT against marketdata.bars
    carries an availability_time predicate."""


def test_no_float_reaches_a_stored_price() -> None:
    """Bar's OHLC fields are Decimal, and the model is strict, so a float is
    a ValidationError rather than a silent binary artifact."""


def test_marketdata_imports_no_broker_module_except_the_boundary_dto() -> None:
```

- [ ] **Step 4: Live tests behind the `mt5` marker**

Reuse the `_skip_reason()` probe from `tests/live/test_mt5_terminal.py`.
Backfill a small real window of H1, assert the recorded `coverage_ratio` is
plausible, and assert the stored bars round-trip through the reader unchanged.

- [ ] **Step 5: Documentation**

- `README.md`: a "Phase 1.5 — market data" section covering the two commands,
  the measured history-depth table, what `coverage()` is for, and the fact that
  M1 holds roughly three months.
- Add **I-17** to the invariant table in `mt5-multi-agent-trading-house-spec.md`.

- [ ] **Step 6: Full verification gate**

```bash
uv lock --check
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run pytest
uv run trading-house constitution verify && uv run trading-house constitution binding
git status --short
```

Expected: every command exits `0`; coverage at or above 95%; `terminal.py`
under its 80-statement cap; no stray artifacts.

- [ ] **Step 7: Commit**

```bash
git add tests README.md mt5-multi-agent-trading-house-spec.md
git commit -m "docs: accept phase 1.5 market data"
```

---

## Final Verification Gate

Run from the repository root without relying on earlier output:

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run trading-house health
git status --short
```

Expected results:

- every command exits `0`;
- coverage at or above 95%;
- `marketdata/` imports no MetaTrader5, enforced by acceptance test;
- no `order_send` anywhere in `src/`;
- the Phase 0, 0.5 and 1 acceptance suites still green;
- `health` still reports ready.

## Deliberately Not In This Plan

| Item | Where it lands |
|---|---|
| Tick storage, microstructure | A later phase, if scalp research demands it |
| Equities, session calendars, corporate actions | Their own phase (D-4) |
| Indicators, features | Phase 2, `features/` |
| The backtest engine | Later; this phase only makes an honest one possible |
| An ingest daemon or scheduler | The orchestration plane (D-3) |
| Non-MT5 data sources | Later; `HistoryProvider` is their seam |
| Outlier and spike adjudication | Research, over the full series (D-7) |
