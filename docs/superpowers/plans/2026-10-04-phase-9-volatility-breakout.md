# Phase 9 — Volatility-Breakout Swing on EURUSD H1: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register a second real strategy — a Bollinger-squeeze volatility breakout on EURUSD H1 — with the features it reads, a backtest scope taken from the registry instead of `cli.py` constants, and a synthetic end-to-end proof, so the Phase 8 pipeline can judge it on real H1 data.

**Architecture:** Pure Bollinger indicators in `features/indicators/bollinger.py`; a typed `BollingerFeatures` block on `FeatureSnapshot`, computed by `FeatureEngine.bollinger` over one fixed 200-bar window and only when a strategy declares `FeatureBlock.BOLLINGER` in its new `required_features`; a `StrategyScope` on each registry entry (instrument, timeframe, exit arms) replacing `_BACKTEST_INSTRUMENT`, `_BACKTEST_TIMEFRAME` and `_EXIT_POLICIES`; and the strategy itself in `strategies/impl/vol_breakout.py`.

**Tech Stack:** Python 3.12, Pydantic v2 (strict, frozen, `extra="forbid"`), `Decimal` for all prices, mypy strict, Ruff, pytest, Hypothesis, Typer, uv.

**Spec:** `docs/superpowers/specs/2026-10-03-phase-9-volatility-breakout-design.md`

## Global Constraints

- Python 3.12. Pydantic v2 strict, frozen, `extra="forbid"`. **All prices and money are `Decimal`; never float.** Analytics fields on `TradeProposal` stay `float`.
- mypy strict; Ruff `["E","F","I","B","UP","SIM","RUF","S","PT"]`; line length 100.
- **`UV_SYSTEM_CERTS=1` prefixes every `uv` command.** `uv run mypy` takes **no path arguments** and does not check the test tree.
- **`strategies/` may import `core/`, `features/`, `risk/` and `strategies/` only** (`tests/acceptance/test_architecture.py:120`). In particular it may **not** import `marketdata` — so `Timeframe` never appears under `strategies/`; a scope carries the timeframe as its string value (`"H1"`) and `cli.py` converts it.
- **`research/backtest/` may import `core/`, `marketdata/`, `features/`, `risk/`** and itself only.
- **`features/` may not import `brokers/` or `risk/`.** Every public function under `features/indicators/` must be annotated `-> Decimal` (`tests/acceptance/test_phase2.py`).
- Every frozen strategy number comes from the spec §4 verbatim: Bollinger 20 bars, 2σ population; squeeze = bandwidth equal to the minimum of the 125 bars ending at it (ties count); squeeze recency 10 bars; trend mean 200 bars; invalidation = middle band; `horizon_seconds = max_holding_seconds = 432_000`; book `fx_swing`; arms `none`, `fixed_target` 2.0R, `chandelier` 3.0 ATR / 10 points; priors 20.0 / 10.0 / 3.0 total cost (1.0 non-swap + 2.0 swap) / 2.0 swap / 0.45 win. **No other values. No sweeps.**
- **Session Momentum must not move.** Its three CLI outputs are pinned in Task 1 and must stay byte-identical through every later task. `tests/unit/research/backtest/test_engine.py:1131-1133` pins the engine's own identity and must also stay green.
- **Do not re-sign or edit `config/risk_constitution.yaml`.** No task needs it. A task that believes it does stops and reports.
- **No credential, DSN, account number or raw broker message in any error, log or payload.** Commands print deterministic key-sorted JSON.
- The system connects to a **demo account only**. No task here touches a terminal; the H1 backfill is an operator step after the plan (see "After the tasks").
- **Commit before mutating, never after.** A mutation workflow assumes a committed base.
- **Every new test earns its place by mutation.** Each test below names the mutation that must break it. Make it, confirm that test (and only tests about that behaviour) fail, restore with `git checkout -- <file>`.
- **Commits are made only with the user's authorization for this execution.** If it is absent, leave each task's file state in place and report the uncommitted diff instead of committing.

## Baseline

The 2026-10-03 full run was `2981 passed, 10 skipped, 3 failed` in 7h12m. The three failures (`tests/property/test_constitution_signatures.py` ×2, `tests/property/test_mark.py` ×1) pass in isolation and are suspected Hypothesis-deadline flakes under load, being fixed in a separate task. They are the only acceptable pre-existing failures.

The full suite is too slow to run per task on this machine. Each task runs its own targeted tests; Task 8 runs `tests/unit tests/acceptance tests/property` once.

Before Task 1, record:

```
UV_SYSTEM_CERTS=1 uv run pytest tests/unit tests/acceptance -q --no-cov -p no:cacheprovider
```

## File Structure

| File | Responsibility |
|---|---|
| `src/trading_house/features/indicators/bollinger.py` | **New.** `simple_mean`, `population_stdev`, `bandwidth`, `squared_bandwidth`. Pure, `Decimal` in and out. |
| `src/trading_house/core/snapshot.py` | Gains `FeatureBlock` and `BollingerFeatures`; `FeatureSnapshot.bollinger` (default `None`). |
| `src/trading_house/features/engine.py` | Gains the five Bollinger constants and `FeatureEngine.bollinger`. |
| `src/trading_house/core/exits.py` | `Strategy` protocol gains `required_features`. |
| `src/trading_house/research/backtest/engine.py` | `_snapshot` computes the block only when the strategy declares it. |
| `src/trading_house/strategies/registry.py` | Entries carry a `StrategyScope`; new `strategy_scope()`. |
| `src/trading_house/strategies/impl/session_momentum.py` | Declares `required_features = frozenset()`. Nothing else changes. |
| `src/trading_house/strategies/impl/vol_breakout.py` | **New.** The strategy and its `StrategySpec`. |
| `src/trading_house/cli.py` | Scope constants and `_EXIT_POLICIES` removed; four call sites read the registry. |
| `tests/unit/research/backtest/conftest.py` | `ToyStrategy.required_features`; `_breakout_h1()` fixture. |
| `tests/unit/features/test_bollinger.py` | **New.** Indicator known answers. |
| `tests/unit/features/test_bollinger_engine.py` | **New.** The block: values, squeeze, recency, warm-up, I-17, I-18. |
| `tests/unit/strategies/test_vol_breakout.py` | **New.** Rule table and purity. |
| `tests/unit/strategies/test_registry.py` | **New.** Scopes. |
| `tests/unit/test_cli.py` | Session Momentum output pins; the new strategy's arm mapping. |
| `tests/acceptance/test_phase9.py` | **New.** Registration, scope, end-to-end H1 replay. |
| `README.md` | Phase 9 section. |

---

### Task 1: Pin Session Momentum's three CLI outputs

**Why first:** every later task touches a path Session Momentum runs through (snapshot, engine, registry, CLI). Its recorded Phase 7 evidence is only reproducible if nothing it emits changes. Pin the bytes now, against the unmodified code.

**Files:**
- Modify: `tests/unit/test_cli.py` (append after `test_the_arms_parameters_are_not_command_line_options`)

**Interfaces:**
- Consumes: the existing `_backtest_args`, `_session_bars`, `runner`, the `_dsn` fixture.
- Produces: `test_session_momentum_backtest_output_is_pinned`, which every later task must keep green.

- [ ] **Step 1: Write the pin test with unpinned values**

```python
_SESSION_MOMENTUM_STDOUT_SHA256: dict[str, str] = {
    "none": "UNPINNED",
    "fixed_target": "UNPINNED",
    "chandelier": "UNPINNED",
}
"""sha256 of ``backtest run``'s whole stdout for Session Momentum on the session ramp,
captured before Phase 9 changed anything. Phase 9 moves the snapshot, the engine, the
registry and the CLI scope; the recorded Phase 7 evidence stays reproducible only if
these bytes never move."""


@pytest.mark.parametrize("arm", ["none", "fixed_target", "chandelier"])
@pytest.mark.usefixtures("_dsn")
def test_session_momentum_backtest_output_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    from tests.unit.research.backtest.conftest import FakeBarReader

    monkeypatch.setattr(cli, "_bar_store", lambda: FakeBarReader(_session_bars()))

    result = runner.invoke(cli.app, _backtest_args(tmp_path, **{"--exit-policy": arm}))

    assert result.exit_code == 0, result.stderr
    actual = hashlib.sha256(result.stdout.encode()).hexdigest()
    assert actual == _SESSION_MOMENTUM_STDOUT_SHA256[arm], f"{arm}: {actual}"
```

Add `import hashlib` to the file's imports if it is not already there.

- [ ] **Step 2: Run it to capture the three digests**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/test_cli.py -k session_momentum_backtest_output_is_pinned -q --no-cov -p no:cacheprovider`
Expected: 3 FAIL, each message `"<arm>: <64 hex chars>"`.

- [ ] **Step 3: Paste the three printed digests into `_SESSION_MOMENTUM_STDOUT_SHA256`**, replacing each `"UNPINNED"` with the value printed for that arm.

- [ ] **Step 4: Run it twice and confirm both runs pass** (proves the output is deterministic, not merely captured once)

Run the Step 2 command twice. Expected: `3 passed` both times.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_cli.py
git commit -m "test: pin session momentum's backtest output before phase 9"
```

- [ ] **Step 6: Mutation proof**

In `src/trading_house/cli.py`, change `_EXIT_POLICIES`'s chandelier `atr_multiple=Decimal("3.0")` to `Decimal("3.1")`. Run the Step 2 command. Expected: exactly the `chandelier` case fails. Restore: `git checkout -- src/trading_house/cli.py`.

---

### Task 2: Bollinger indicators

**Files:**
- Create: `src/trading_house/features/indicators/bollinger.py`
- Test: `tests/unit/features/test_bollinger.py`

**Interfaces:**
- Consumes: `trading_house.core.errors.InsufficientHistoryError`.
- Produces:
  - `simple_mean(values: Sequence[Decimal]) -> Decimal`
  - `population_stdev(values: Sequence[Decimal]) -> Decimal`
  - `bandwidth(values: Sequence[Decimal], width: Decimal) -> Decimal` — `(upper − lower) / middle` = `2·width·σ / mean`
  - `squared_bandwidth(values: Sequence[Decimal], width: Decimal) -> Decimal` — `4·width²·variance / mean²`, no square root
  - Every function raises `InsufficientHistoryError` on an empty sequence.

- [ ] **Step 1: Write the failing tests**

```python
from decimal import Decimal

import pytest

from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.indicators.bollinger import (
    bandwidth,
    population_stdev,
    simple_mean,
    squared_bandwidth,
)

TWO = Decimal(2)
ALTERNATING = [Decimal("1.1000"), Decimal("1.1002")] * 10
"""Twenty closes alternating 0.0001 either side of 1.1001: population sigma is
exactly 0.0001, so every expected value below is exact rather than rounded."""


def test_simple_mean_is_the_arithmetic_mean() -> None:
    assert simple_mean([Decimal(1), Decimal(2), Decimal(3), Decimal(4)]) == Decimal("2.5")


def test_population_stdev_divides_by_n_not_n_minus_one() -> None:
    """The textbook population example: mean 5, variance 4. Sample deviation
    (n - 1) would give 2.138..., so this pins which one Bollinger's bands use."""

    values = [Decimal(v) for v in (2, 4, 4, 4, 5, 5, 7, 9)]
    assert population_stdev(values) == Decimal(2)


def test_bandwidth_is_band_width_over_middle() -> None:
    assert bandwidth(ALTERNATING, TWO) == Decimal("0.0004") / Decimal("1.1001")


def test_squared_bandwidth_is_bandwidth_squared_without_a_root() -> None:
    assert squared_bandwidth(ALTERNATING, TWO) == Decimal("0.00000016") / (
        Decimal("1.1001") * Decimal("1.1001")
    )


@pytest.mark.parametrize("function", [simple_mean, population_stdev])
def test_an_empty_series_is_insufficient_history(function: object) -> None:
    with pytest.raises(InsufficientHistoryError):
        function([])  # type: ignore[operator]


@pytest.mark.parametrize("function", [bandwidth, squared_bandwidth])
def test_an_empty_band_is_insufficient_history(function: object) -> None:
    with pytest.raises(InsufficientHistoryError):
        function([], TWO)  # type: ignore[operator]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features/test_bollinger.py -q --no-cov -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: trading_house.features.indicators.bollinger`.

- [ ] **Step 3: Write the module**

```python
"""Bollinger bands over closes. Pure: closes in, a Decimal out.

These feed only a strategy's entry test and its invalidation price, which the
risk engine then turns into a stop distance -- so like ``volatility.py`` the
correctness bar is the execution path's. Population deviation (divide by n),
because that is Bollinger's own definition and the strategy spec fixes it.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from trading_house.core.errors import InsufficientHistoryError


def simple_mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise InsufficientHistoryError
    return sum(values, start=Decimal(0)) / len(values)


def _population_variance(values: Sequence[Decimal]) -> Decimal:
    mean = simple_mean(values)
    return sum(((value - mean) ** 2 for value in values), start=Decimal(0)) / len(values)


def population_stdev(values: Sequence[Decimal]) -> Decimal:
    return _population_variance(values).sqrt()


def bandwidth(values: Sequence[Decimal], width: Decimal) -> Decimal:
    """``(upper - lower) / middle``, which is ``2 * width * sigma / mean``."""

    return 2 * width * population_stdev(values) / simple_mean(values)


def squared_bandwidth(values: Sequence[Decimal], width: Decimal) -> Decimal:
    """``bandwidth`` squared, computed without a square root.

    The squeeze test compares 135 bandwidths per bar to find a minimum. The
    ordering of non-negative numbers survives squaring, so the comparison can
    skip ``sqrt`` entirely -- and an exact rational comparison is also one that
    cannot disagree with itself in the last digit.
    """

    mean = simple_mean(values)
    return 4 * width * width * _population_variance(values) / (mean * mean)
```

- [ ] **Step 4: Run the tests and the indicator acceptance test**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features/test_bollinger.py tests/acceptance/test_phase2.py -q --no-cov -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trading_house/features/indicators/bollinger.py tests/unit/features/test_bollinger.py
git commit -m "feat: phase 9 bollinger indicators"
```

- [ ] **Step 6: Mutation proof** — one at a time, restoring after each:
  - In `_population_variance`, divide by `len(values) - 1`. Expected: `test_population_stdev_divides_by_n_not_n_minus_one` and both bandwidth tests fail.
  - In `bandwidth`, drop the `2 *`. Expected: `test_bandwidth_is_band_width_over_middle` fails, the squared test does not.
  - In `squared_bandwidth`, replace `(mean * mean)` with `mean`. Expected: only the squared test fails.

---

### Task 3: The Bollinger block on the snapshot and the engine

**Files:**
- Modify: `src/trading_house/core/snapshot.py`
- Modify: `src/trading_house/features/engine.py`
- Test: `tests/unit/features/test_bollinger_engine.py`

**Interfaces:**
- Consumes: Task 2's four functions.
- Produces:
  - `trading_house.core.snapshot.FeatureBlock` — `str` enum, one member `BOLLINGER = "bollinger"`.
  - `trading_house.core.snapshot.BollingerFeatures` — `CanonicalModel` with `middle`, `upper`, `lower`, `bandwidth`, `previous_close`, `previous_upper`, `previous_lower`, `sma_200` (all `Decimal`) and `bars_since_squeeze: NonNegativeInt | None`.
  - `FeatureSnapshot.bollinger: BollingerFeatures | None = None`.
  - `trading_house.features.engine` constants `BOLLINGER_PERIOD = 20`, `BOLLINGER_WIDTH = Decimal(2)`, `SQUEEZE_LOOKBACK = 125`, `SQUEEZE_RECENCY = 10`, `TREND_PERIOD = 200`, `BOLLINGER_WINDOW = 200`.
  - `FeatureEngine.bollinger(instrument_id: str, timeframe: Timeframe, *, as_of: datetime) -> BollingerFeatures`.

**Background.** `bars_since_squeeze` is `last − j*`, where `j*` is the latest bar in `[last − 10, last]` whose squared bandwidth is `<=` the minimum squared bandwidth of the 125 bars ending at it. The scan therefore needs squared bandwidths for indices `last − 134 … last`, and the earliest of those needs closes from `last − 153`. `sma_200` needs 200 closes. So one window of 200 bars covers both. It is read through the existing `_window`, which excludes defective bars and raises `InsufficientHistoryError` when fewer than 200 clean bars are knowable at `as_of`.

- [ ] **Step 1: Write the failing tests**

```python
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.unit.research.backtest.conftest import FakeBarReader
from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.engine import FeatureEngine
from trading_house.marketdata.models import Bar, BarQuality, Timeframe

START = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
BIG = Decimal("0.0010")
TINY = Decimal("0.00001")


def _h1(closes: Sequence[Decimal], *, start: datetime = START) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.H1,
            event_time=start + timedelta(hours=index),
            availability_time=start + timedelta(hours=index + 1),
            open=close,
            high=close + Decimal("0.0001"),
            low=close - Decimal("0.0001"),
            close=close,
            tick_volume=100,
            spread=10,
            real_volume=0,
            quality=BarQuality.OK,
        )
        for index, close in enumerate(closes)
    )


def _alternating(count: int, amplitude: Decimal, *, offset: int = 0) -> list[Decimal]:
    """Closes alternating ``amplitude`` either side of 1.1. Index parity is taken
    from ``offset + i`` so two segments joined end to end keep alternating."""

    return [
        Decimal("1.1") + (amplitude if (offset + i) % 2 else -amplitude) for i in range(count)
    ]


def _block(bars: tuple[Bar, ...], *, as_of: datetime | None = None):  # type: ignore[no-untyped-def]
    engine = FeatureEngine(FakeBarReader(bars))
    return engine.bollinger(
        "fx.eurusd",
        Timeframe.H1,
        as_of=as_of if as_of is not None else bars[199].availability_time,
    )


def test_the_block_has_the_hand_computed_values() -> None:
    """Every window alternates 0.0001 either side of 1.1001, so sigma is
    exactly 0.0001, the bands are exactly 1.1003 / 1.0999, and every bar's
    bandwidth is equal -- a tie, which counts as a squeeze, so the last bar
    is one and ``bars_since_squeeze`` is zero."""

    closes = [Decimal("1.1000"), Decimal("1.1002")] * 100
    block = _block(_h1(closes))

    assert block.middle == Decimal("1.1001")
    assert block.upper == Decimal("1.1003")
    assert block.lower == Decimal("1.0999")
    assert block.bandwidth == Decimal("0.0004") / Decimal("1.1001")
    assert block.previous_close == Decimal("1.1000")
    assert block.previous_upper == Decimal("1.1003")
    assert block.previous_lower == Decimal("1.0999")
    assert block.sma_200 == Decimal("1.1001")
    assert block.bars_since_squeeze == 0


def test_a_squeeze_ten_bars_back_is_counted() -> None:
    """Big swings, then a tiny-swing run whose last bar is index 189, then big
    swings again. Bar 189's 20-bar window (170..189) is all tiny, so it is the
    125-bar minimum; every later window holds big swings, so nothing after it
    is. 199 - 189 = 10, the edge of the recency window."""

    closes = _alternating(170, BIG) + _alternating(20, TINY, offset=170)
    closes += _alternating(10, BIG, offset=190)
    assert _block(_h1(closes)).bars_since_squeeze == 10


def test_a_squeeze_eleven_bars_back_is_not() -> None:
    """The same shape shifted one bar earlier: the tiny run ends at 188, so the
    latest squeeze is eleven bars back -- outside the window -- and bar 189's
    window already holds a big swing."""

    closes = _alternating(169, BIG) + _alternating(20, TINY, offset=169)
    closes += _alternating(11, BIG, offset=189)
    assert _block(_h1(closes)).bars_since_squeeze is None


def test_expanding_volatility_has_no_squeeze() -> None:
    closes = _alternating(170, TINY) + _alternating(30, BIG, offset=170)
    assert _block(_h1(closes)).bars_since_squeeze is None


def test_fewer_than_two_hundred_bars_is_insufficient_history() -> None:
    bars = _h1([Decimal("1.1000"), Decimal("1.1002")] * 100)[:199]
    engine = FeatureEngine(FakeBarReader(bars))
    with pytest.raises(InsufficientHistoryError):
        engine.bollinger("fx.eurusd", Timeframe.H1, as_of=bars[-1].availability_time)


def test_the_same_instant_gives_the_same_block_however_much_history_is_stored() -> None:
    """I-18: fifty extra older bars behind the same 200 change nothing."""

    closes = _alternating(170, BIG) + _alternating(20, TINY, offset=170)
    closes += _alternating(10, BIG, offset=190)
    shallow = _h1(closes)
    deep = _h1(_alternating(50, BIG) + closes, start=START - timedelta(hours=50))

    assert _block(shallow) == _block(deep, as_of=shallow[199].availability_time)


def test_a_bar_not_yet_available_cannot_move_the_block() -> None:
    """I-17: a wild bar knowable only after ``as_of`` is invisible."""

    closes = [Decimal("1.1000"), Decimal("1.1002")] * 100
    bars = _h1(closes)
    later = _h1([Decimal("1.5000")], start=START + timedelta(hours=200))

    assert _block(bars + later, as_of=bars[199].availability_time) == _block(bars)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features/test_bollinger_engine.py -q --no-cov -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'FeatureEngine' object has no attribute 'bollinger'`.

- [ ] **Step 3: Add the block types to `core/snapshot.py`**

Add `from enum import Enum` to the imports, and above `class FeatureSnapshot`:

```python
class FeatureBlock(str, Enum):  # noqa: UP042
    """An optional feature block a strategy can ask the backtester to compute.

    Optional rather than always present because each block costs a fixed window
    of reads per bar, and a strategy that never looks at it should neither pay
    for it nor have its snapshots changed by it.
    """

    BOLLINGER = "bollinger"


class BollingerFeatures(CanonicalModel):
    """Bollinger bands (20 bars, 2 population sigma), the previous bar's bands,
    the bars since the last squeeze, and the 200-bar trend mean.

    ``bars_since_squeeze`` is ``None`` when no bar in the last ten was a
    squeeze -- distinct from zero, which means this bar is one.
    """

    middle: Decimal
    upper: Decimal
    lower: Decimal
    bandwidth: Decimal
    previous_close: Decimal
    previous_upper: Decimal
    previous_lower: Decimal
    bars_since_squeeze: NonNegativeInt | None
    sma_200: Decimal
```

And as the last field of `FeatureSnapshot`, after `bars_since_session_open`:

```python
    bollinger: BollingerFeatures | None = None
```

- [ ] **Step 4: Add the constants and the method to `features/engine.py`**

Imports to add:

```python
from typing import Final

from trading_house.core.snapshot import BollingerFeatures
from trading_house.features.indicators.bollinger import (
    population_stdev,
    simple_mean,
    squared_bandwidth,
)
```

Constants, after `SPAN_SAFETY`:

```python
BOLLINGER_PERIOD: Final[int] = 20
BOLLINGER_WIDTH: Final[Decimal] = Decimal(2)
SQUEEZE_LOOKBACK: Final[int] = 125
SQUEEZE_RECENCY: Final[int] = 10
TREND_PERIOD: Final[int] = 200
BOLLINGER_WINDOW: Final[int] = max(
    TREND_PERIOD, BOLLINGER_PERIOD + SQUEEZE_LOOKBACK + SQUEEZE_RECENCY - 1
)
"""One fixed window for the whole block (I-18): 200 for the trend mean, and
the squeeze scan needs only 154. The Phase 9 spec freezes every number here;
changing one is a new strategy version and a new trial, not a tweak."""
```

Method, after `bars_since_session_open`:

```python
    def bollinger(
        self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime
    ) -> BollingerFeatures:
        """The Bollinger block over the last ``BOLLINGER_WINDOW`` bars at ``as_of``."""

        closes = [
            bar.close
            for bar in self._window(
                instrument_id, timeframe, count=BOLLINGER_WINDOW, as_of=as_of
            )
        ]
        last = len(closes) - 1

        def band(index: int) -> list[Decimal]:
            return closes[index - BOLLINGER_PERIOD + 1 : index + 1]

        first_scanned = last - SQUEEZE_RECENCY - SQUEEZE_LOOKBACK + 1
        squared = {
            index: squared_bandwidth(band(index), BOLLINGER_WIDTH)
            for index in range(first_scanned, last + 1)
        }

        def is_squeeze(index: int) -> bool:
            lookback = range(index - SQUEEZE_LOOKBACK + 1, index + 1)
            return squared[index] <= min(squared[k] for k in lookback)

        squeezes = [i for i in range(last - SQUEEZE_RECENCY, last + 1) if is_squeeze(i)]
        middle, sigma = simple_mean(band(last)), population_stdev(band(last))
        previous_middle = simple_mean(band(last - 1))
        previous_sigma = population_stdev(band(last - 1))
        offset = BOLLINGER_WIDTH * sigma
        previous_offset = BOLLINGER_WIDTH * previous_sigma
        return BollingerFeatures(
            middle=middle,
            upper=middle + offset,
            lower=middle - offset,
            bandwidth=2 * offset / middle,
            previous_close=closes[last - 1],
            previous_upper=previous_middle + previous_offset,
            previous_lower=previous_middle - previous_offset,
            bars_since_squeeze=last - squeezes[-1] if squeezes else None,
            sma_200=simple_mean(closes[-TREND_PERIOD:]),
        )
```

- [ ] **Step 5: Run the new tests, the snapshot tests and the architecture tests**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features tests/unit/research/backtest/test_snapshot.py tests/acceptance/test_architecture.py tests/acceptance/test_phase2.py tests/property/test_schema_boundaries.py -q --no-cov -p no:cacheprovider`
Expected: all pass. If `test_schema_boundaries.py` reports `BollingerFeatures` missing from a registry of models, add a builder entry for it in that file's `BUILDERS` beside `FeatureSnapshot`, using the values from `test_the_block_has_the_hand_computed_values`.

- [ ] **Step 6: Run Task 1's pin** — `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/test_cli.py -k pinned -q --no-cov -p no:cacheprovider`. Expected: `3 passed`.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/core/snapshot.py src/trading_house/features/engine.py tests/unit/features/test_bollinger_engine.py
git commit -m "feat: phase 9 bollinger feature block on the engine and snapshot"
```

- [ ] **Step 8: Mutation proof** — one at a time, restoring after each:
  - `is_squeeze`: `<=` → `<`. Expected: `test_the_block_has_the_hand_computed_values` fails (ties stop counting).
  - `squeezes` range: `last - SQUEEZE_RECENCY` → `last - SQUEEZE_RECENCY - 1`. Expected: `test_a_squeeze_eleven_bars_back_is_not` fails.
  - `previous_close=closes[last - 1]` → `closes[last]`. Expected: the hand-computed test fails.
  - `sma_200=simple_mean(closes[-TREND_PERIOD:])` → `closes[-BOLLINGER_PERIOD:]`. Expected: no current test fails, because the hand-computed series has the same mean over any even window. **Add** this test, confirm it fails under the mutation, restore, confirm it passes:

```python
def test_the_trend_mean_spans_two_hundred_bars() -> None:
    closes = [Decimal("1.0")] * 180 + [Decimal("1.1000"), Decimal("1.1002")] * 10
    assert _block(_h1(closes)).sma_200 == (Decimal("1.0") * 180 + Decimal("22.002")) / 200
```

---

### Task 4: Strategies declare the blocks they need

**Files:**
- Modify: `src/trading_house/core/exits.py` (`Strategy` protocol)
- Modify: `src/trading_house/research/backtest/engine.py` (`_snapshot`)
- Modify: `src/trading_house/strategies/impl/session_momentum.py`
- Modify: `tests/unit/research/backtest/conftest.py` (`ToyStrategy`)
- Test: `tests/unit/research/backtest/test_engine.py` (append)

**Interfaces:**
- Consumes: Task 3's `FeatureBlock`, `FeatureEngine.bollinger`.
- Produces: `Strategy.required_features: frozenset[FeatureBlock]`; `ToyStrategy.required_features` (default `frozenset()`); the engine fills `snapshot.bollinger` exactly when `FeatureBlock.BOLLINGER in strategy.required_features`.

- [ ] **Step 1: Give `ToyStrategy` the field** (the engine will read it; without it every engine test breaks in Step 4)

In `tests/unit/research/backtest/conftest.py`, add to the imports `from trading_house.core.snapshot import FeatureBlock`, and in `ToyStrategy` after `side`:

```python
    required_features: frozenset[FeatureBlock] = frozenset()
    """Which optional feature blocks the backtester computes for this toy. Empty
    by default, like Session Momentum, so every existing engine test keeps the
    snapshots -- and the pinned identities -- it had before Phase 9."""
```

- [ ] **Step 2: Write the failing engine tests** (append to `tests/unit/research/backtest/test_engine.py`)

```python
# --- Phase 9: optional feature blocks ------------------------------------------


def test_a_strategy_that_declares_bollinger_gets_the_block_from_its_first_snapshot() -> None:
    """The block needs 200 bars, so the first snapshot is bar 199's -- not bar
    20's, which is when ATR alone would allow one."""

    strategy = ToyStrategy(every_n=10_000, required_features=frozenset({FeatureBlock.BOLLINGER}))
    _run(bars=_ramp(260), strategy=strategy)

    assert len(strategy.seen) == 260 - 199
    assert all(snapshot.bollinger is not None for snapshot in strategy.seen)


def test_a_strategy_that_declares_nothing_gets_no_block() -> None:
    strategy = ToyStrategy(every_n=10_000)
    _run(bars=_ramp(260), strategy=strategy)

    assert len(strategy.seen) == 260 - FIRST_SNAPSHOT_BAR
    assert all(snapshot.bollinger is None for snapshot in strategy.seen)
```

Add `from trading_house.core.snapshot import FeatureBlock` to the file's imports; `FIRST_SNAPSHOT_BAR`, `ToyStrategy`, `_ramp` and `_run` come from the conftest the file already imports from.

- [ ] **Step 3: Run them to verify the first fails**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest/test_engine.py -k "bollinger or declares_nothing" -q --no-cov -p no:cacheprovider`
Expected: the first FAILS (`len(seen) == 240`, blocks `None`); the second passes.

- [ ] **Step 4: Extend the protocol, the engine and Session Momentum**

`core/exits.py` — add `from trading_house.core.snapshot import FeatureBlock, FeatureSnapshot` (replacing the existing `FeatureSnapshot` import) and in `class Strategy(Protocol)` after `max_holding_seconds: int`:

```python
    required_features: frozenset[FeatureBlock]
```

`research/backtest/engine.py` — import `FeatureBlock` from `trading_house.core.snapshot`. Inside `_snapshot`'s `try`, after `bars_since_session_open = ...`:

```python
            bollinger = (
                features.bollinger(request.instrument_id, request.timeframe, as_of=as_of)
                if FeatureBlock.BOLLINGER in request.strategy.required_features
                else None
            )
```

and pass `bollinger=bollinger,` as the last argument of the `FeatureSnapshot(...)` call. Add one sentence to the `_snapshot` docstring: "The Bollinger block's own warm-up raises ``InsufficientHistoryError`` too and is skipped the same way; it is computed only for a strategy that declares it, so no other strategy's snapshots change."

`strategies/impl/session_momentum.py` — import `FeatureBlock` from `trading_house.core.snapshot`, and after `max_holding_seconds`:

```python
    required_features: Final[frozenset[FeatureBlock]] = frozenset()
```

- [ ] **Step 5: Run the backtest unit suite, Session Momentum's tests, and the pins**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest tests/unit/strategies tests/acceptance/test_phase6.py tests/acceptance/test_phase7.py -q --no-cov -p no:cacheprovider`
Then: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/test_cli.py -k pinned -q --no-cov -p no:cacheprovider`
Then: `UV_SYSTEM_CERTS=1 uv run mypy`
Expected: all pass, including `test_a_constant_notional_run_keeps_its_pre_change_identity`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/core/exits.py src/trading_house/research/backtest/engine.py src/trading_house/strategies/impl/session_momentum.py tests/unit/research/backtest/conftest.py tests/unit/research/backtest/test_engine.py
git commit -m "feat: phase 9 strategies declare the feature blocks they need"
```

- [ ] **Step 7: Mutation proof** — replace the conditional with an unconditional `features.bollinger(...)`. Expected: `test_a_strategy_that_declares_nothing_gets_no_block` fails (240 → 61 snapshots, blocks present). Restore.

---

### Task 5: Backtest scope from the registry

**Files:**
- Modify: `src/trading_house/strategies/registry.py`
- Modify: `src/trading_house/cli.py` (lines 179-199, `_instrument_contract` ~1415, `_backtest_request` ~1442, callers at ~1324, ~2231, ~2340, ~2643, holdout coverage ~2632)
- Test: `tests/unit/strategies/test_registry.py`

**Interfaces:**
- Consumes: `NoExitPolicy`, `FixedTargetPolicy`, `ChandelierPolicy`, `ExitPolicy` from `core.exits`.
- Produces:
  - `StrategyScope(CanonicalModel)`: `instrument_id: InstrumentId`, `timeframe: NonEmptyStr` (a `Timeframe` value), `fixed_target: FixedTargetPolicy`, `chandelier: ChandelierPolicy`; method `exit_arm(name: str) -> ExitPolicy` for `"none"`, `"fixed_target"`, `"chandelier"`, raising `ConfigurationError` otherwise.
  - `strategy_scope(strategy_id: str) -> StrategyScope`, raising `ConfigurationError` for an unregistered id.
  - `registered()` and `REGISTERED_STRATEGY_IDS` keep their signatures.
  - In `cli.py`: `_instrument_contract(contract: Path, *, strategy_id: str)`.

- [ ] **Step 1: Write the failing registry tests**

```python
from decimal import Decimal

import pytest

from trading_house.core.errors import ConfigurationError
from trading_house.core.exits import ChandelierPolicy, FixedTargetPolicy, NoExitPolicy
from trading_house.marketdata.models import Timeframe
from trading_house.strategies.registry import REGISTERED_STRATEGY_IDS, strategy_scope


def test_session_momentum_keeps_the_scope_the_cli_constants_gave_it() -> None:
    """Exactly the values ``cli.py`` hard-coded before Phase 9; anything else
    would move its pinned outputs."""

    scope = strategy_scope("session_momentum_eurusd")

    assert scope.instrument_id == "fx.eurusd"
    assert scope.timeframe == "M15"
    assert scope.exit_arm("none") == NoExitPolicy(kind="none")
    assert scope.exit_arm("fixed_target") == FixedTargetPolicy(
        kind="fixed_target", r_multiple=Decimal("1.0")
    )
    assert scope.exit_arm("chandelier") == ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
    )


@pytest.mark.parametrize("strategy_id", sorted(REGISTERED_STRATEGY_IDS))
def test_every_scope_names_a_real_timeframe(strategy_id: str) -> None:
    """``strategies/`` may not import ``Timeframe``, so the scope carries the
    string; this is where a typo in it would be caught rather than at a run."""

    Timeframe(strategy_scope(strategy_id).timeframe)


def test_an_unregistered_strategy_has_no_scope() -> None:
    with pytest.raises(ConfigurationError):
        strategy_scope("nope")


def test_an_unknown_arm_is_refused() -> None:
    with pytest.raises(ConfigurationError):
        strategy_scope("session_momentum_eurusd").exit_arm("martingale")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/strategies/test_registry.py -q --no-cov -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'strategy_scope'`.

- [ ] **Step 3: Rewrite `strategies/registry.py`**

```python
"""The registry of strategies that have a complete specification, and the one
place each strategy's backtest scope is declared.

The scope -- instrument, timeframe and the parameters of the three exit arms --
lives beside the strategy rather than in ``cli.py`` so a second strategy can
exist at all, and stays off the command line for the reason it always was: an
operator who can choose the data after seeing an outcome is running a sweep.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, cast

from trading_house.core.errors import ConfigurationError
from trading_house.core.exits import (
    ChandelierPolicy,
    ExitPolicy,
    FixedTargetPolicy,
    NoExitPolicy,
    Strategy,
)
from trading_house.core.values import CanonicalModel, InstrumentId, NonEmptyStr
from trading_house.strategies.impl.session_momentum import (
    SESSION_MOMENTUM_ID,
    SESSION_MOMENTUM_SPEC,
    SessionMomentum,
)
from trading_house.strategies.spec import StrategySpec


class StrategyScope(CanonicalModel):
    instrument_id: InstrumentId
    timeframe: NonEmptyStr
    """A ``Timeframe`` value. A string because ``strategies/`` may not import
    ``marketdata``; ``cli.py`` converts it, and a test proves every one converts."""

    fixed_target: FixedTargetPolicy
    chandelier: ChandelierPolicy

    def exit_arm(self, name: str) -> ExitPolicy:
        if name == "none":
            return NoExitPolicy(kind="none")
        if name == "fixed_target":
            return self.fixed_target
        if name == "chandelier":
            return self.chandelier
        raise ConfigurationError()


@dataclass(frozen=True, slots=True)
class _Entry:
    spec: StrategySpec
    factory: Callable[[ExitPolicy | None], object]
    scope: StrategyScope


_SWING_CHANDELIER: Final = ChandelierPolicy(
    kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
)

_REGISTRY: Final[dict[str, _Entry]] = {
    SESSION_MOMENTUM_ID: _Entry(
        spec=SESSION_MOMENTUM_SPEC,
        factory=SessionMomentum,
        scope=StrategyScope(
            instrument_id="fx.eurusd",
            timeframe="M15",
            fixed_target=FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.0")),
            chandelier=_SWING_CHANDELIER,
        ),
    ),
}
REGISTERED_STRATEGY_IDS: Final[frozenset[str]] = frozenset(_REGISTRY)


def _entry(strategy_id: str) -> _Entry:
    try:
        return _REGISTRY[strategy_id]
    except KeyError:
        raise ConfigurationError() from None


def registered(strategy_id: str, *, exit_policy: ExitPolicy | None = None) -> Strategy:
    return cast(Strategy, _entry(strategy_id).factory(exit_policy))


def strategy_scope(strategy_id: str) -> StrategyScope:
    return _entry(strategy_id).scope
```

- [ ] **Step 4: Run the registry tests** — Step 2's command. Expected: `5 passed` (4 tests, one parametrized over the one registered id).

- [ ] **Step 5: Move `cli.py` onto the registry**

1. Delete `_BACKTEST_INSTRUMENT`, `_BACKTEST_TIMEFRAME`, `_EXIT_POLICIES` and `_exit_policy` (lines 179-199). Keep `class ExitPolicyName`.
2. Add `from trading_house.strategies.registry import strategy_scope` to the imports. Remove any import Ruff then reports unused (likely `NoExitPolicy`, `FixedTargetPolicy`, `ChandelierPolicy`).
3. `_instrument_contract` — new signature and last check:

```python
def _instrument_contract(contract: Path, *, strategy_id: str) -> InstrumentContract:
```

```python
    if instrument_contract.instrument_id != strategy_scope(strategy_id).instrument_id:
        raise ConfigurationError()
    return instrument_contract
```

   Update its docstring's first paragraph: "The contract file, decoded and checked against the strategy's registered instrument."
4. `_backtest_request` — inside the `try`, before `return BacktestRequest(`:

```python
        scope = strategy_scope(strategy)
```

   and in the `BacktestRequest(...)` call:

```python
            strategy=build_strategy(strategy, exit_policy=scope.exit_arm(exit_policy.value)),
            instrument_id=scope.instrument_id,
            timeframe=Timeframe(scope.timeframe),
```

5. Callers of `_instrument_contract`:
   - `backtest_run` (~1324): `_instrument_contract(contract, strategy_id=strategy)`
   - the three `_RunInputs(...)` constructions (~2231, ~2340, ~2643): `_instrument_contract(contract, strategy_id=parsed.strategy_id)`
6. Holdout coverage (~2632):

```python
        scope = strategy_scope(registered.strategy_id)
        refuse_outside_coverage(
            start, end, _bar_store().coverage(scope.instrument_id, Timeframe(scope.timeframe))
        )
```

7. Confirm nothing is left: `git grep -n "_BACKTEST_\|_EXIT_POLICIES\|_exit_policy(" src` prints nothing.

- [ ] **Step 6: Run the CLI suite, the registry tests, the pins, the acceptance tests that cover these commands, and mypy**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/test_cli.py tests/unit/strategies tests/acceptance/test_phase7.py tests/acceptance/test_phase8b2b.py tests/acceptance/test_phase8b3.py tests/acceptance/test_phase8d2.py -q --no-cov -p no:cacheprovider`
Then: `UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run ruff format --check . && UV_SYSTEM_CERTS=1 uv run mypy`
Expected: all pass, the three pins included.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/strategies/registry.py src/trading_house/cli.py tests/unit/strategies/test_registry.py
git commit -m "feat: phase 9 take the backtest scope and exit arms from the registry"
```

- [ ] **Step 8: Mutation proof** — one at a time, restoring after each:
  - Registry: Session Momentum's `timeframe="M15"` → `"H1"`. Expected: `test_session_momentum_keeps_the_scope_the_cli_constants_gave_it` and all three pins fail.
  - Registry: Session Momentum's fixed target `"1.0"` → `"2.0"`. Expected: the scope test, the `fixed_target` pin and `test_each_cli_arm_builds_its_exact_predeclared_policy[fixed_target]` fail.
  - `_instrument_contract`: drop the `!=` check. Expected: the existing CLI test that refuses a contract for another instrument fails. If none exists, add this test to `tests/unit/test_cli.py`, confirm it fails under the mutation, restore, confirm it passes:

```python
@pytest.mark.usefixtures("_dsn")
def test_backtest_refuses_a_contract_for_another_instrument(tmp_path: Path) -> None:
    from tests.unit.research.backtest.conftest import _contract

    path = tmp_path / "gbp.json"
    path.write_text(_contract(instrument_id="fx.gbpusd").model_dump_json(), encoding="utf-8")

    result = runner.invoke(cli.app, _backtest_args(tmp_path, **{"--contract": str(path)}))

    _assert_redacted_configuration_error(result)
```

  (`_contract(**overrides)` at `tests/unit/research/backtest/conftest.py:103` takes `instrument_id` as an override.)

---

### Task 6: The strategy, its spec, and its registration

**Files:**
- Create: `src/trading_house/strategies/impl/vol_breakout.py`
- Modify: `src/trading_house/strategies/registry.py` (one entry)
- Test: `tests/unit/strategies/test_vol_breakout.py`
- Modify: `tests/unit/strategies/test_registry.py` (one test)

**Interfaces:**
- Consumes: `BollingerFeatures`, `FeatureBlock`, `FeatureSnapshot` (Task 3), `StrategyScope` (Task 5).
- Produces: `VOL_BREAKOUT_ID = "vol_breakout_eurusd_h1"`, `VOL_BREAKOUT_SPEC: StrategySpec`, `class VolBreakout` with `__init__(self, exit_policy: ExitPolicy | None = None)`, `evaluate`, `exit_policy`.

- [ ] **Step 1: Write the failing strategy tests**

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_house.core.exits import FixedTargetPolicy
from trading_house.core.schemas import Side
from trading_house.core.snapshot import BollingerFeatures, FeatureBlock, FeatureSnapshot
from trading_house.features.sessions import session_of
from trading_house.marketdata.models import Bar, BarQuality, Timeframe
from trading_house.strategies.impl.vol_breakout import VOL_BREAKOUT_ID, VolBreakout

LONDON_BAR = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)
ROLLOVER_BAR = datetime(2026, 9, 16, 22, 0, tzinfo=UTC)


def _block(**overrides: object) -> BollingerFeatures:
    """A block for a clean long breakout at close 1.10700: above the upper
    band, previous close inside its band, above the trend, squeeze one bar ago."""

    values: dict[str, object] = {
        "middle": Decimal("1.10415"),
        "upper": Decimal("1.10546"),
        "lower": Decimal("1.10284"),
        "bandwidth": Decimal("0.0024"),
        "previous_close": Decimal("1.10405"),
        "previous_upper": Decimal("1.10410"),
        "previous_lower": Decimal("1.10390"),
        "bars_since_squeeze": 1,
        "sma_200": Decimal("1.10260"),
    }
    values.update(overrides)
    return BollingerFeatures(**values)  # type: ignore[arg-type]


def _snapshot(
    *,
    close: Decimal = Decimal("1.10700"),
    block: BollingerFeatures | None = None,
    event_time: datetime = LONDON_BAR,
    timeframe: Timeframe = Timeframe.H1,
    instrument_id: str = "fx.eurusd",
    with_block: bool = True,
) -> FeatureSnapshot:
    as_of = event_time + timedelta(hours=1)
    bar = Bar(
        instrument_id=instrument_id,
        timeframe=timeframe,
        event_time=event_time,
        availability_time=as_of,
        open=close,
        high=close + Decimal("0.0001"),
        low=close - Decimal("0.0001"),
        close=close,
        tick_volume=100,
        spread=10,
        real_volume=0,
        quality=BarQuality.OK,
    )
    return FeatureSnapshot(
        as_of=as_of,
        instrument_id=instrument_id,
        timeframe=timeframe,
        bar=bar,
        atr=Decimal("0.00040"),
        median_spread_points=Decimal(10),
        tick_spread_points=Decimal(10),
        tick_time=as_of,
        session=session_of(event_time),
        prior_session_return=None,
        session_open_price=close,
        bars_since_session_open=0,
        bollinger=(block if block is not None else _block()) if with_block else None,
    )


def _short_block(**overrides: object) -> BollingerFeatures:
    values: dict[str, object] = {
        "middle": Decimal("1.10385"),
        "upper": Decimal("1.10516"),
        "lower": Decimal("1.10254"),
        "previous_close": Decimal("1.10395"),
        "previous_upper": Decimal("1.10410"),
        "previous_lower": Decimal("1.10390"),
        "sma_200": Decimal("1.10540"),
    }
    values.update(overrides)
    return _block(**values)


def test_it_declares_the_bollinger_block() -> None:
    assert VolBreakout().required_features == frozenset({FeatureBlock.BOLLINGER})


def test_a_fresh_cross_above_the_upper_band_in_an_uptrend_goes_long() -> None:
    proposal = VolBreakout().evaluate(_snapshot())

    assert proposal is not None
    assert proposal.side is Side.BUY
    assert proposal.strategy_id == VOL_BREAKOUT_ID
    assert proposal.book == "fx_swing"
    assert proposal.entry_price_ref == Decimal("1.10700")
    assert proposal.invalidation_price == Decimal("1.10415")
    assert proposal.max_holding_seconds == 432_000
    assert proposal.horizon_seconds == 432_000


def test_a_fresh_cross_below_the_lower_band_in_a_downtrend_goes_short() -> None:
    proposal = VolBreakout().evaluate(
        _snapshot(close=Decimal("1.10100"), block=_short_block())
    )

    assert proposal is not None
    assert proposal.side is Side.SELL
    assert proposal.invalidation_price == Decimal("1.10385")


@pytest.mark.parametrize(
    ("label", "close", "block"),
    [
        ("continuation, previous close already above", None, _block(previous_close=Decimal("1.10420"))),
        ("long against the trend", None, _block(sma_200=Decimal("1.10800"))),
        ("no squeeze in the last ten bars", None, _block(bars_since_squeeze=None)),
        ("close inside the band", Decimal("1.10500"), None),
        ("short against the trend", Decimal("1.10100"), _short_block(sma_200=Decimal("1.10000"))),
        ("short continuation", Decimal("1.10100"), _short_block(previous_close=Decimal("1.10380"))),
    ],
)
def test_no_entry(label: str, close: Decimal | None, block: BollingerFeatures | None) -> None:
    snapshot = _snapshot(
        close=close if close is not None else Decimal("1.10700"),
        block=block,
    )
    assert VolBreakout().evaluate(snapshot) is None, label


def test_no_entry_in_the_rollover_window() -> None:
    assert VolBreakout().evaluate(_snapshot(event_time=ROLLOVER_BAR)) is None


def test_no_entry_without_the_block() -> None:
    assert VolBreakout().evaluate(_snapshot(with_block=False)) is None


@pytest.mark.parametrize(
    ("instrument_id", "timeframe"),
    [("fx.gbpusd", Timeframe.H1), ("fx.eurusd", Timeframe.M15)],
)
def test_no_entry_outside_eurusd_h1(instrument_id: str, timeframe: Timeframe) -> None:
    snapshot = _snapshot(instrument_id=instrument_id, timeframe=timeframe)
    assert VolBreakout().evaluate(snapshot) is None


def test_the_fixed_target_arm_stamps_its_multiple() -> None:
    policy = FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("2.0"))
    proposal = VolBreakout(policy).evaluate(_snapshot())

    assert proposal is not None
    assert proposal.target_r_multiple == Decimal("2.0")


def test_the_same_snapshot_gives_the_same_proposal() -> None:
    """§10.2: ``evaluate`` is a pure function of the snapshot."""

    snapshot = _snapshot()
    assert VolBreakout().evaluate(snapshot) == VolBreakout().evaluate(snapshot)
```

Append to `tests/unit/strategies/test_registry.py`:

```python
def test_the_breakout_is_registered_on_eurusd_h1_with_its_own_arms() -> None:
    scope = strategy_scope("vol_breakout_eurusd_h1")

    assert scope.instrument_id == "fx.eurusd"
    assert scope.timeframe == "H1"
    assert scope.exit_arm("fixed_target") == FixedTargetPolicy(
        kind="fixed_target", r_multiple=Decimal("2.0")
    )
    assert scope.exit_arm("chandelier") == ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
    )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/strategies -q --no-cov -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: trading_house.strategies.impl.vol_breakout`.

- [ ] **Step 3: Write the strategy**

```python
"""Bollinger-squeeze volatility breakout on EURUSD H1."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from trading_house.core.exits import ExitPolicy, FixedTargetPolicy, NoExitPolicy
from trading_house.core.schemas import Side, TradeProposal
from trading_house.core.snapshot import FeatureBlock, FeatureSnapshot
from trading_house.core.values import BookId, PositiveQuantity
from trading_house.features.sessions import Session
from trading_house.strategies.spec import StrategySpec

VOL_BREAKOUT_ID: Final[str] = "vol_breakout_eurusd_h1"

VOL_BREAKOUT_SPEC: Final[StrategySpec] = StrategySpec(
    economic_rationale=(
        "Volatility clusters: when EURUSD's 20-bar dispersion contracts to a local minimum, "
        "the expansion that follows tends to resolve in one direction as positioning unwinds, "
        "and a break that agrees with the 200-bar trend has the larger pool of followers. "
        "A hypothesis to be falsified, not a claim of edge."
    ),
    universe=("fx.eurusd",),
    trading_horizon="H1 bars; holds of up to five calendar days, weekends permitted.",
    entry_rule=(
        "Bollinger 20 bars, 2 population sigma. A squeeze bar's bandwidth equals the minimum of "
        "the 125 bars ending at it (ties count). Long when a squeeze occurred within the last 10 "
        "bars, the close is above the upper band, the previous close was at or below its own "
        "upper band, and the close is above the 200-bar mean; short is the mirror. No entry on "
        "bars opening 21:00-24:00 UTC."
    ),
    exit_rule=(
        "Invalidation at the middle band; the risk-engine stop; a 432000-second time stop; "
        "and the selected exit arm."
    ),
    cost_model_description=(
        "Commission 0.0 per lot per side (official FBS publishes no commission); "
        "slippage 0.4 points per side (predeclared prior); "
        "swap long -7.7 points/day and short +2.0 points/day (terminal audit); "
        "triple swap Wednesday; stress multiplier 1; defective tolerance 0."
    ),
    capacity_model="Capacity is not modelled and is explicitly a non-promise.",
    invalidation=(
        "Not yet tested. Invalidated if the three-arm trial loses money after costs in every "
        "arm, or if the Phase 8 gates reject it; no tuning or promotion follows a rejection."
    ),
    regime_constraints=(
        "The risk spread, spread-to-stop and tick-staleness gates apply; no entries in the "
        "21:00-24:00 UTC rollover window, which is fixed in UTC and carries the DST limitation."
    ),
    trail_decision="Pending the three-arm A/B: none, fixed_target 2.0R, chandelier 3.0 ATR.",
    trial_count=3,
    versioning=(
        "strategy=vol_breakout_eurusd_h1; version=1; "
        "constitution_sha256=a87e63fb8c46912b1bc21bae3e55535b88ae4abc613cb87988399c4c28e5d58b; "
        "contract_sha256=59121ba95a21afb81e48f4de9c9358705375c678954fa2249dbbd80b41b86f90"
    ),
)


class VolBreakout:
    """Trade the first close outside the Bollinger band after a squeeze, with
    the 200-bar trend.

    Every number is frozen by the Phase 9 spec, section 4. Changing one is a
    new version and a new trial.
    """

    def __init__(self, exit_policy: ExitPolicy | None = None) -> None:
        self._exit_policy = NoExitPolicy(kind="none") if exit_policy is None else exit_policy

    id: Final[str] = VOL_BREAKOUT_ID
    version: Final[str] = "1"
    book: Final[BookId] = "fx_swing"
    horizon_seconds: Final[int] = 432_000
    max_holding_seconds: Final[int] = 432_000
    required_features: Final[frozenset[FeatureBlock]] = frozenset({FeatureBlock.BOLLINGER})
    expected_return_bps: Final[float] = 20.0
    expected_return_stdev_bps: Final[float] = 10.0
    expected_cost_bps: Final[float] = 3.0
    expected_swap_cost_bps: Final[float] = 2.0
    win_probability: Final[float] = 0.45

    def evaluate(self, snapshot: FeatureSnapshot) -> TradeProposal | None:
        if snapshot.instrument_id != "fx.eurusd" or snapshot.timeframe.value != "H1":
            return None
        if snapshot.session is Session.OFF:
            return None
        block = snapshot.bollinger
        if block is None or block.bars_since_squeeze is None:
            return None

        close = snapshot.bar.close
        if (
            close > block.upper
            and block.previous_close <= block.previous_upper
            and close > block.sma_200
        ):
            side = Side.BUY
        elif (
            close < block.lower
            and block.previous_close >= block.previous_lower
            and close < block.sma_200
        ):
            side = Side.SELL
        else:
            return None

        return TradeProposal(
            source=self.id,
            event_time=snapshot.as_of,
            availability_time=snapshot.as_of,
            processing_time=snapshot.as_of,
            proposal_id=(
                f"{self.id}:{snapshot.instrument_id}:{snapshot.timeframe.value}:"
                f"{snapshot.as_of.isoformat()}"
            ),
            strategy_id=self.id,
            strategy_version=self.version,
            book=self.book,
            instrument_id=snapshot.instrument_id,
            side=side,
            horizon_seconds=self.horizon_seconds,
            entry_condition="bollinger-squeeze-breakout",
            entry_price_ref=close,
            invalidation_price=block.middle,
            max_holding_seconds=self.max_holding_seconds,
            expected_return_bps=self.expected_return_bps,
            expected_return_stdev_bps=self.expected_return_stdev_bps,
            expected_cost_bps=self.expected_cost_bps,
            expected_swap_cost_bps=self.expected_swap_cost_bps,
            win_probability=self.win_probability,
            calibration_id="vol-breakout-prior-v1",
            required_liquidity=PositiveQuantity(amount=Decimal(1), unit="lots"),
            regime_ref="squeeze-with-200-bar-trend",
            features_snapshot_id=f"snapshot:{snapshot.instrument_id}:{snapshot.as_of.isoformat()}",
            target_r_multiple=(
                self._exit_policy.r_multiple
                if isinstance(self._exit_policy, FixedTargetPolicy)
                else None
            ),
        )

    def exit_policy(self) -> ExitPolicy:
        return self._exit_policy
```

- [ ] **Step 4: Register it** — in `strategies/registry.py`, import `VOL_BREAKOUT_ID`, `VOL_BREAKOUT_SPEC`, `VolBreakout` from `trading_house.strategies.impl.vol_breakout` and add to `_REGISTRY`:

```python
    VOL_BREAKOUT_ID: _Entry(
        spec=VOL_BREAKOUT_SPEC,
        factory=VolBreakout,
        scope=StrategyScope(
            instrument_id="fx.eurusd",
            timeframe="H1",
            fixed_target=FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("2.0")),
            chandelier=_SWING_CHANDELIER,
        ),
    ),
```

- [ ] **Step 5: Run the strategy tests, the architecture guard and mypy**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/strategies tests/acceptance/test_architecture.py tests/acceptance/test_phase7.py -q --no-cov -p no:cacheprovider`
Then: `UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run ruff format --check . && UV_SYSTEM_CERTS=1 uv run mypy`
Expected: one failure, `tests/acceptance/test_phase7.py::test_the_registry_contains_the_real_strategy_and_not_the_toy`, whose first line asserts the registry is exactly `{SESSION_MOMENTUM_ID}`. That claim was about Phase 7's own strategy, not the registry's size. Replace that line with:

```python
    assert SESSION_MOMENTUM_ID in REGISTERED_STRATEGY_IDS
```

keep its `"toy"` line, and rerun. Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/strategies/impl/vol_breakout.py src/trading_house/strategies/registry.py tests/unit/strategies/test_vol_breakout.py tests/unit/strategies/test_registry.py
git commit -m "feat: phase 9 volatility-breakout strategy on EURUSD H1"
```

- [ ] **Step 7: Mutation proof** — one at a time, restoring after each:
  - Delete `and block.previous_close <= block.previous_upper`. Expected: the "continuation" case fails.
  - Delete `and close > block.sma_200`. Expected: "long against the trend" fails.
  - Delete the `Session.OFF` check. Expected: `test_no_entry_in_the_rollover_window` fails.
  - `invalidation_price=block.middle` → `block.lower`. Expected: the long test fails (and `TradeProposal` may refuse the short — either way a test fails).
  - `"H1"` → `"M15"` in `evaluate`. Expected: every entry test fails.

---

### Task 7: End-to-end on a synthetic H1 series

**Files:**
- Modify: `tests/unit/research/backtest/conftest.py` (add `_breakout_h1`)
- Create: `tests/acceptance/test_phase9.py`
- Modify: `tests/unit/test_cli.py` (one parametrized arm-mapping test)

**Interfaces:**
- Consumes: everything above; `FakeBarReader`, `_contract`, `runner`, `_backtest_args`, `_dsn`.
- Produces: `_breakout_h1() -> tuple[Bar, ...]`, 360 H1 bars with exactly one long breakout, at bar 230.

**The fixture, and why it trades exactly once.** Bars start Monday 2026-09-07 00:00 UTC. Bars 0–199 rise 0.00002 per bar from 1.10000 and alternate ±0.00080, so σ ≈ 0.0008 and no close reaches 2σ. Bars 200–229 sit at 1.10400 ± 0.00005, so bars 219–229 have the smallest bandwidth in their 125-bar lookback (ties, all squeezes). Bar 229's band is 1.10400 ± 0.00010 and it closes at 1.10405 — inside. Bar 230 (2026-09-16 14:00, London) closes at 1.10700. Its band is about 1.10415 ± 0.00131, so it is above the upper band, its previous close was inside, and the 200-bar mean (about 1.1026) is below it — a long. Entry fills at bar 231's open. Bars 231–359 sit at 1.10700 ± 0.00005, which never reaches the 2.0R target, so every arm closes on the 432,000-second time stop at bar 351. Once more than four flat bars are in the window the upper band sits above 1.10705, so there is no second fresh cross.

- [ ] **Step 1: Add the fixture** to `tests/unit/research/backtest/conftest.py`:

```python
def _breakout_h1() -> tuple[Bar, ...]:
    """360 H1 bars with exactly one long volatility breakout, at bar 230.

    See the Phase 9 plan, Task 7, for the arithmetic: a wide rising swing,
    a 30-bar squeeze at 1.10400, a close at 1.10700, and a flat tail long enough
    for the five-day time stop to close the trade inside the series.
    """

    origin = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    closes: list[Decimal] = []
    for index in range(360):
        if index < 200:
            swing = Decimal("0.00080") if index % 2 else Decimal("-0.00080")
            closes.append(Decimal("1.10000") + Decimal("0.00002") * index + swing)
        elif index < 230:
            closes.append(Decimal("1.10400") + (POINT * 5 if index % 2 else -POINT * 5))
        elif index == 230:
            closes.append(Decimal("1.10700"))
        else:
            closes.append(Decimal("1.10700") + (POINT * 5 if index % 2 else -POINT * 5))
    return tuple(
        Bar(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.H1,
            event_time=origin + timedelta(hours=index),
            availability_time=origin + timedelta(hours=index + 1),
            open=close,
            high=close + POINT * 10,
            low=close - POINT * 10,
            close=close,
            tick_volume=100,
            spread=RAMP_SPREAD_POINTS,
            real_volume=0,
            quality=BarQuality.OK,
        )
        for index, close in enumerate(closes)
    )
```

- [ ] **Step 2: Write the acceptance test**

```python
"""Phase 9 acceptance: a second strategy, registered with a complete spec, scoped
to EURUSD H1 by the registry, replayed end to end through ``backtest run``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.unit.research.backtest.conftest import FakeBarReader, _breakout_h1, _contract
from trading_house import cli
from trading_house.marketdata.models import Timeframe
from trading_house.strategies.impl.vol_breakout import VOL_BREAKOUT_ID, VOL_BREAKOUT_SPEC
from trading_house.strategies.registry import REGISTERED_STRATEGY_IDS, strategy_scope

runner = CliRunner()


@pytest.fixture
def _dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same fake DSN ``tests/unit/test_cli.py`` uses: ``backtest run`` reads
    settings, never connects. A local fixture because ``tests/conftest.py``
    already has a helper function called ``_dsn``."""

    from tests.unit.test_cli import FAKE_DSN

    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", FAKE_DSN)


def test_the_breakout_is_registered_with_a_complete_spec() -> None:
    assert VOL_BREAKOUT_ID in REGISTERED_STRATEGY_IDS
    assert VOL_BREAKOUT_SPEC.universe == ("fx.eurusd",)
    assert VOL_BREAKOUT_SPEC.trial_count == 3


def test_the_registry_scopes_it_to_eurusd_h1() -> None:
    scope = strategy_scope(VOL_BREAKOUT_ID)
    assert (scope.instrument_id, Timeframe(scope.timeframe)) == ("fx.eurusd", Timeframe.H1)


def _args(tmp_path: Path, arm: str) -> list[str]:
    contract = tmp_path / "contract.json"
    contract.write_text(_contract().model_dump_json(), encoding="utf-8")
    bars = _breakout_h1()
    return [
        "backtest", "run",
        "--strategy", VOL_BREAKOUT_ID,
        "--exit-policy", arm,
        "--start", bars[0].event_time.isoformat(),
        "--end", bars[-1].event_time.isoformat(),
        "--firm-equity", "100000",
        "--contract", str(contract),
        "--atr-period", "14",
        "--spread-window", "10",
        "--commission-per-lot-per-side", "0",
        "--slippage-points-per-side", "0.4",
        "--swap-long-points-per-day", "-7.7",
        "--swap-short-points-per-day", "2.0",
        "--triple-swap-weekday", "2",
    ]


@pytest.mark.parametrize("arm", ["none", "fixed_target", "chandelier"])
@pytest.mark.usefixtures("_dsn")
def test_every_arm_replays_the_synthetic_breakout_to_exactly_one_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    monkeypatch.setattr(cli, "_bar_store", lambda: FakeBarReader(_breakout_h1()))

    result = runner.invoke(cli.app, _args(tmp_path, arm))

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)["result"]
    assert payload["timeframe"] == "H1"
    assert payload["strategy_id"] == VOL_BREAKOUT_ID
    assert len(payload["trades"]) == 1, payload["rejections"]
    assert payload["trades"][0]["side"] == "BUY"
```

`SimulatedTrade.side` is a `Side`, serialised as `"BUY"` / `"SELL"` (checked against `research/backtest/result.py:38` and `core/schemas.py:52`).

- [ ] **Step 3: Add the arm-mapping test** to `tests/unit/test_cli.py`, beside `test_each_cli_arm_builds_its_exact_predeclared_policy`:

```python
@pytest.mark.parametrize(
    ("arm", "expected"),
    [
        ("none", NoExitPolicy(kind="none")),
        ("fixed_target", FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("2.0"))),
        (
            "chandelier",
            ChandelierPolicy(
                kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
            ),
        ),
    ],
)
@pytest.mark.usefixtures("_dsn")
def test_the_breakout_cli_arms_build_its_own_predeclared_policies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arm: str,
    expected: NoExitPolicy | FixedTargetPolicy | ChandelierPolicy,
) -> None:
    from tests.unit.research.backtest.conftest import FakeBarReader, _breakout_h1
    from trading_house.ops.backtest import build_strategy

    captured: list[NoExitPolicy | FixedTargetPolicy | ChandelierPolicy] = []

    def capture(
        strategy_id: str,
        *,
        exit_policy: NoExitPolicy | FixedTargetPolicy | ChandelierPolicy,
    ) -> Any:
        captured.append(exit_policy)
        return build_strategy(strategy_id, exit_policy=exit_policy)

    bars = _breakout_h1()
    monkeypatch.setattr(cli, "build_strategy", capture)
    monkeypatch.setattr(cli, "_bar_store", lambda: FakeBarReader(bars))

    result = runner.invoke(
        cli.app,
        _backtest_args(
            tmp_path,
            **{
                "--strategy": "vol_breakout_eurusd_h1",
                "--exit-policy": arm,
                "--start": bars[0].event_time.isoformat(),
                "--end": bars[-1].event_time.isoformat(),
                "--atr-period": "14",
            },
        ),
    )

    assert result.exit_code == 0, result.stderr
    assert captured == [expected]
```

- [ ] **Step 4: Run them**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/acceptance/test_phase9.py tests/unit/test_cli.py -q --no-cov -p no:cacheprovider`
Expected: all pass. **If the end-to-end test finds zero trades,** the assertion message prints the run's rejection reasons. Diagnose from them; do **not** change any strategy number to make the fixture trade. A fixture change is allowed only if it keeps the plan's described shape (one squeeze, one fresh long cross at bar 230) and the rejection names the fixture's construction (for example a spread or margin gate tripped by the synthetic contract), and the change and its reason are reported.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/research/backtest/conftest.py tests/acceptance/test_phase9.py tests/unit/test_cli.py
git commit -m "test: phase 9 acceptance, the breakout replayed end to end on synthetic H1"
```

- [ ] **Step 6: Mutation proof** — one at a time, restoring after each:
  - `features/engine.py`: `SQUEEZE_RECENCY` `10` → `0`. Expected: the end-to-end test finds zero trades (the squeeze was one bar before the breakout).
  - Registry: the breakout's fixed target `"2.0"` → `"1.0"`. Expected: `test_the_breakout_cli_arms_build_its_own_predeclared_policies[fixed_target]` and the registry test fail.

---

### Task 8: README, spec status, and whole-suite verification

**Files:**
- Modify: `README.md` (new Phase 9 section after the Phase 8e section; banner line updated)
- Modify: `docs/superpowers/specs/2026-10-03-phase-9-volatility-breakout-design.md` (`**Status:**`)

- [ ] **Step 1: Add the README section**

~~~markdown
## Phase 9 — Volatility-breakout swing on EURUSD H1

Phase 9 registers the second strategy, `vol_breakout_eurusd_h1` (design:
[Phase 9](docs/superpowers/specs/2026-10-03-phase-9-volatility-breakout-design.md)).
It is a hypothesis, not a claim of edge, and **has not yet been run on real
data.**

**The rule.** Bollinger bands of 20 H1 closes at 2 population sigma. A squeeze
bar's bandwidth equals the minimum of the 125 bars ending at it. Go long when a
squeeze happened within the last 10 bars, the close crosses above the upper band
from inside it, and the close is above the 200-bar mean; short is the mirror.
No entries on bars opening 21:00–24:00 UTC. Invalidation is the middle band, the
book is `fx_swing`, and the time stop is five calendar days. The exit A/B is
`none`, `fixed_target` at 2.0R, and `chandelier` at 3.0 ATR with a 10-point
step — three trials.

**Scope comes from the registry.** Each registered strategy declares its
instrument, timeframe and exit-arm parameters in `strategies/registry.py`.
`backtest run`, `research trial scenarios`, `compounding` and `open-holdout`
read them from there; none of them is a command-line option.

**Features are opt-in.** A strategy names the feature blocks it needs in
`required_features`. Only the breakout asks for `bollinger`, so Session
Momentum's snapshots and its recorded results are unchanged.

### Running it

The H1 history must be stored first:

```bash
uv run trading-house data backfill --instrument fx.eurusd --timeframe H1 --from 2010-01-01
```

Then register the trial and its protocol **before any real backtest**, and run
the Phase 8 sequence — `scenarios`, `validate`, `decide`, and `open-holdout`
only on `RESEARCH_PASSED` — with `--strategy vol_breakout_eurusd_h1`. A loss
completes the phase: no tuning, and no rerun under the same id.
~~~

Update the top banner's first paragraph to say Phase 9 registers a second strategy, a volatility breakout on EURUSD H1, still untested on real data. Keep every other banner sentence.

- [ ] **Step 2: Set the spec's status** — `**Status:** approved design` → `**Status:** implemented; evidence pending`.

- [ ] **Step 3: Whole-suite verification**

Run:
```
UV_SYSTEM_CERTS=1 uv run ruff format --check .
UV_SYSTEM_CERTS=1 uv run ruff check .
UV_SYSTEM_CERTS=1 uv run mypy
UV_SYSTEM_CERTS=1 uv run pytest tests/unit tests/acceptance tests/property -q -p no:cacheprovider
```
Expected: clean format, lint and types; the pytest run passes with coverage at or above 95%. The only acceptable failures are the three Hypothesis-deadline flakes named in the Baseline, and only if each one passes when rerun alone. Integration tests (`tests/integration`) need Docker and are run by CI.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-10-03-phase-9-volatility-breakout-design.md
git commit -m "docs: phase 9 README and spec status"
```

---

## After the tasks: the evidence run (operator steps, not code)

In this order, so the protocol is locked before any outcome is seen (spec §9):

1. **Fix the demo terminal login.** The 2026-10-03 run reported `Terminal: Authorization failed`.
2. **Backfill:** `uv run trading-house data backfill --instrument fx.eurusd --timeframe H1 --from 2010-01-01`. Record the stored span, bar counts and run id in the README, as Phase 7 did.
3. **Digest the dataset** with `research dataset digest` over the declared window and holdout.
4. **Register** the trial and its protocol (`research trial register`), declaring window, holdout, cost grid and the three arms.
5. **Run** `research trial scenarios` for each arm, then `validate` and `decide`; `open-holdout` only on `RESEARCH_PASSED`.
6. **Record the outcome** in the README and in `VOL_BREAKOUT_SPEC.invalidation` and `trail_decision` (a new commit, as Phase 7's result was).

## Self-Review

- **Spec coverage:** §4.2 rule → Task 6; §4.3 arms → Tasks 5–6; §4.4 priors → Task 6 (gate arithmetic 19 bps ≥ 2 and 10.5% ≤ 20% is in the spec); §5 features → Tasks 2–3; §6 snapshot and port → Tasks 3–4; §6 Session Momentum byte-identical → Task 1 pins plus the existing engine identity pins; §7 registry scope → Task 5; §8 data and §9 protocol → "After the tasks"; §10 testing → each task's tests plus Task 7; README → Task 8.
- **Placeholder scan:** the only deliberately unfilled values are Task 1's three digests, which Step 2 captures and Step 3 pastes against unmodified code.
- **Type consistency:** `FeatureBlock.BOLLINGER`, `BollingerFeatures` (nine fields), `FeatureEngine.bollinger(instrument_id, timeframe, *, as_of)`, `Strategy.required_features: frozenset[FeatureBlock]`, `StrategyScope.exit_arm(name: str)`, `strategy_scope(strategy_id)`, `_instrument_contract(contract, *, strategy_id)`, `VOL_BREAKOUT_ID`, `VOL_BREAKOUT_SPEC`, `VolBreakout(exit_policy)` and `_breakout_h1()` are spelled the same in every task that uses them.
