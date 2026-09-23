# Phase 7 — The First Real Strategy: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship one real strategy — session-conditioned directional bias on EURUSD — with the features it reads, the gates that judge its declared economics, the exits it can express, and the evidence §10.3 demands.

**Architecture:** Four new session features computed by `FeatureEngine` and carried as named typed fields on `FeatureSnapshot`; a new `strategies/` package holding a validated `StrategySpec`, a registry and the one strategy; three constitution limits enforced in `RiskEngine.evaluate`; a fixed target unblocked in the risk engine and a trailing stop added to the simulator; and an A/B over three pre-declared exit arms.

**Tech Stack:** Python 3.12, Pydantic v2 (strict, frozen, `extra="forbid"`), `Decimal` for all money and prices, mypy strict, Ruff, pytest, Hypothesis, uv, PostgreSQL 18.

**Spec:** `docs/superpowers/specs/2026-09-22-phase-7-session-momentum-design.md`

## Global Constraints

- Python 3.12. Pydantic v2 strict, frozen, `extra="forbid"`. **All money and prices are `Decimal`; never float.** Analytics use `FiniteFloat`.
- mypy strict; Ruff `["E","F","I","B","UP","SIM","RUF","S","PT"]`; line length 100.
- **`UV_SYSTEM_CERTS=1` prefixes every `uv` command** (a TLS-inspecting proxy). `uv run mypy` takes **no path arguments** and does not check the test tree.
- **`strategies/` may import `core/`, `features/` and `risk/`. Not `brokers/`, `execution/` or `research/`.**
- **`research/backtest/` may import `core/`, `marketdata/`, `features/` and `risk/`. Not `brokers/` or `execution/`.** It must call neither `compute_volume` nor `stop_price`.
- **No credential, DSN, account number or raw broker message in any error, log, audit or result payload.** Every command prints deterministic key-sorted JSON on stdout, errors on stderr, and no command prints a path, a credential, or a key.
- The system connects to a **demo account only**.
- **The constitution is already amended and re-signed** at commit `247ba55`: `min_expected_edge_after_cost_bps` is now on `SwingLimits` as well as `ScalpLimits`, set to `2.0` in all three swing books. **Do not re-sign the constitution.** The Ed25519 private key is not in this worktree and no task needs it. A task that believes it needs to change `config/risk_constitution.yaml` should stop and report instead.
- **Commit before mutating, never after.** A mutation workflow assumes a committed base; with an uncommitted one, `git checkout --` turns "restore" into "delete".
- **Every new test earns its place by mutation.** Break the code it protects, confirm that test and only that test fails, restore. Fourteen tests in this repository have passed for reasons unrelated to their claims.

## Baseline

`1029 passed, 3 skipped` for `tests/unit tests/acceptance`; `133 passed` for `tests/integration`; `220 passed` for `tests/unit/constitution tests/property`.

**Task 1 fixes a pre-existing failure** that appears only when `tests/unit` and `tests/property` run in one process. Until Task 1 lands, expect exactly one failure from `tests/property/test_schema_boundaries.py::test_every_datetime_bearing_canonical_model_has_a_builder`.

## File Structure

| File | Responsibility |
|---|---|
| `src/trading_house/features/indicators/session.py` | **New.** `Session` enum and the pure window arithmetic. No bar access. |
| `src/trading_house/features/engine.py` | Gains three session feature methods. Still the only holder of the bar store. |
| `src/trading_house/research/backtest/snapshot.py` | `FeatureSnapshot` gains four named fields. |
| `src/trading_house/research/backtest/fills.py` | Fill timestamps corrected (D-9). |
| `src/trading_house/research/backtest/strategy.py` | `TrailPolicy` becomes `ExitPolicy` with three variants. |
| `src/trading_house/research/backtest/engine.py` | Trailing, the exit-policy branch, the defective-bar tolerance, the corrected deadline arithmetic. |
| `src/trading_house/research/backtest/result.py` | `BacktestResult` gains `defective_bars`. |
| `src/trading_house/core/schemas.py` | `TradeProposal` gains `target_r_multiple`. |
| `src/trading_house/risk/engine.py` | Three new gates; `take_profit_price` computed instead of `None`. |
| `src/trading_house/strategies/spec.py` | **New.** `StrategySpec`: §10.3's twelve items, validated. |
| `src/trading_house/strategies/registry.py` | **New.** The real registry. |
| `src/trading_house/strategies/impl/session_momentum.py` | **New.** The strategy. |
| `src/trading_house/ops/backtest.py` | Composition root: the real registry replaces the toy; the A/B arms are selectable. |
| `src/trading_house/cli.py` | `backtest run` gains `--exit-policy`; the toy options go. |
| `tests/property/test_schema_boundaries.py` | Discovery made import-independent (Task 1). |
| `tests/acceptance/test_architecture.py` | The `strategies/` arrow. |
| `tests/acceptance/test_phase7.py` | **New.** Spec completeness, the gates, the arrow's guard-the-guard. |

---

### Task 1: Close the I-10 hole and make its guard import-independent

**Why first:** `test_naive_datetimes_are_rejected_everywhere` is currently **vacuous for every Phase 6 model**, and every later task in this plan adds or changes a datetime-bearing model. Fixing the guard first means each later task is actually checked.

**Files:**
- Modify: `tests/property/test_schema_boundaries.py` (`_canonical_models`, `BUILDERS`)
- Modify: `README.md` (the documented test invocation)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: a `BUILDERS` dict containing every datetime-bearing leaf `CanonicalModel`, and a `_canonical_models()` that finds models regardless of import order.

**Background the implementer needs.** `_canonical_models()` walks `CanonicalModel.__subclasses__()`. That is *runtime* discovery: a subclass exists only once its module has been imported. Running `tests/property` alone never imports `research/backtest/`, so Phase 6's three models are invisible and the guard reports clean. They appear only when `tests/unit` runs first in the same process.

- [ ] **Step 1: Prove the hole exists, and record the proof**

Run:
```
UV_SYSTEM_CERTS=1 uv run pytest tests/property -q --no-cov -k builder
UV_SYSTEM_CERTS=1 uv run pytest tests/unit tests/property -q --no-cov -k builder
```

Expected: the first PASSES, the second FAILS naming `BacktestResult`, `FeatureSnapshot`, `SimulatedTrade`. Quote both in your report — the same test, two answers, is the finding.

- [ ] **Step 2: Make discovery import-independent**

Replace `_canonical_models`'s body so it imports every module under the package first. A model cannot hide from the guard by being unimported:

```python
def _import_every_module() -> None:
    """Import the whole package so subclass discovery cannot miss a model.

    ``CanonicalModel.__subclasses__()`` only sees classes whose module has been
    imported. That made this file's guard answer differently depending on which
    other tests shared the process -- it passed alone and failed after
    ``tests/unit``, and Phase 6 shipped three unregistered models through the
    gap. Importing the package first removes the dependency on test ordering.
    """

    import trading_house

    for module in pkgutil.walk_packages(trading_house.__path__, f"{trading_house.__name__}."):
        importlib.import_module(module.name)


def _canonical_models() -> set[type[BaseModel]]:
    _import_every_module()
    pending: list[type[BaseModel]] = [CanonicalModel]
    found: set[type[BaseModel]] = set()
    while pending:
        for subclass in pending.pop().__subclasses__():
            found.add(subclass)
            pending.append(subclass)
    return found
```

Add `import importlib` and `import pkgutil` to the module's imports.

- [ ] **Step 3: Run the guard alone and watch it now fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/property -q --no-cov -k builder`
Expected: FAIL naming the same three models. The guard now tells the truth in isolation.

- [ ] **Step 4: Register the three models in `BUILDERS`**

Follow the dict's existing style exactly. Each builder constructs a minimal valid instance. The three models and the datetime fields that make them qualify: `FeatureSnapshot` (`as_of`, `tick_time`), `SimulatedTrade` (`entry_at`, `exit_at`), `BacktestResult` (`start`, `end`). Read each model to get its required fields; reuse existing builders for nested models where the dict already has them.

- [ ] **Step 5: Run the naive-datetime test and confirm it now covers them**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/property -q --no-cov`
Expected: PASS, with `test_naive_datetimes_are_rejected_everywhere` now parametrized over three more models than before. Report the before and after parameter counts.

- [ ] **Step 6: Mutation — prove the naive-datetime check is no longer vacuous for them**

Pick one of the three models and remove the UTC-awareness validation its datetime field relies on (the shared `_PointInTime`/`Stamped` validator, or the field's own). Confirm the parametrized case for that model now fails, where before Task 1 it did not exist. Restore.

- [ ] **Step 7: Fix the documented test invocation**

`README.md` documents the test commands. Add `tests/property` to the invocation that runs `tests/unit tests/acceptance`, so the combination that exposes an ordering-dependent guard is the one people run. State in one sentence why the trees run together.

- [ ] **Step 8: Commit**

```bash
git add tests/property/test_schema_boundaries.py README.md
git commit -m "fix(tests): a model cannot hide from the naive-datetime guard by being unimported"
```

---

### Task 2: Correct the fill timestamps (D-9)

**Why second:** it moves numbers. Doing it before the strategy exists means every later known-answer figure is computed once, against corrected stamps.

**Files:**
- Modify: `src/trading_house/research/backtest/fills.py`
- Modify: `src/trading_house/research/backtest/engine.py` (the deadline comparison)
- Test: `tests/unit/research/backtest/test_fills.py`, `tests/unit/research/backtest/test_engine.py`, `tests/unit/research/backtest/conftest.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Fill.at` means the instant of the price, not the instant the bar became readable.

**The defect.** `entry_fill` prices at `bar.open` but stamps `at=bar.availability_time`, one bar duration later. Three consequences: `SimulatedTrade.entry_at`/`exit_at` misreport the fill instant; `engine.py`'s deadline is computed from that stamp and compared against `bar.event_time`, so `max_holding_seconds` is honoured as **H plus one bar**; and `swap_cost` receives a date pair shifted by one bar, moving the rollover count at day boundaries.

There is no point-in-time argument for the late stamp: the entry bar's `event_time` equals the producing snapshot's `as_of`, so `event_time` is already sound.

- [ ] **Step 1: Write the failing test**

```python
def test_a_fill_is_stamped_at_the_instant_of_its_price() -> None:
    """``at`` is when the fill happened, not when the bar became readable.

    ``entry_fill`` prices at the bar's OPEN, so stamping availability_time --
    one whole bar later -- misreports the fill instant, and the engine's
    deadline arithmetic inherits the error as a hold one bar longer than the
    strategy declared.
    """

    bar = _bar(event_time=datetime(2026, 9, 21, 9, 0, tzinfo=UTC), open=Decimal("1.10000"))

    fill = entry_fill(bar=bar, side=Side.BUY, contract=_contract(), model=_zero_slip())

    assert fill.at == bar.event_time
    assert fill.at != bar.availability_time
```

- [ ] **Step 2: Run it and watch it fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest/test_fills.py -q --no-cov -k instant`
Expected: FAIL — `at` is `availability_time`.

- [ ] **Step 3: Correct both fill sites**

In `fills.py`, every `Fill(...)` construction takes `at=bar.event_time`. Replace the comment that defers this to Phase 7 with one stating what `at` now means and why `event_time` is sound (the entry bar's `event_time` equals the producing snapshot's `as_of`).

- [ ] **Step 4: Correct the deadline arithmetic**

In `engine.py`, `deadline` is derived from the entry fill's `at`. It is compared against `bar.event_time`. Both sides are now `event_time`, so the comparison is consistent — verify by reading, and fix the `HOLDING_SECONDS` docstring in `conftest.py`, which currently explains the off-by-one-bar behaviour as intended.

- [ ] **Step 5: Re-derive every moved number by hand**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research tests/unit/ops -q --no-cov`

Expect failures in the known-answer tests. For each, **re-derive the expected value from the ramp's closed form and the fill rule** — `ramp_price(index)` and `HALF_SPREAD` are defined in the test module for exactly this. **Do not paste what the code now prints.** Show the derivation for each changed constant in your report.

- [ ] **Step 6: Mutation**

Revert `at=bar.event_time` to `at=bar.availability_time` at the entry site only. Confirm the Step 1 test fails. Restore.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/research/backtest/ tests/unit/research/ tests/unit/ops/
git commit -m "fix(backtest): stamp a fill at the instant of its price"
```

---

### Task 3: Session windows

**Files:**
- Create: `src/trading_house/features/indicators/session.py`
- Test: `tests/unit/features/indicators/test_session.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class Session(str, Enum)` with members `ASIAN = "asian"`, `LONDON = "london"`, `NEW_YORK = "new_york"`, `OFF = "off"`
  - `def session_of(moment: datetime) -> Session`
  - `def session_bounds(moment: datetime, session: Session) -> tuple[datetime, datetime]`
  - `def preceding_session_window(as_of: datetime) -> tuple[Session, datetime, datetime]`

**Definitions, fixed in UTC (spec D-2).** Asian `00:00–07:00`, London `07:00–16:00`, New York `12:00–21:00`, everything else `OFF`. Half-open intervals: a moment at exactly `07:00` is London, not Asian. London and New York overlap between `12:00` and `16:00`; `session_of` resolves the overlap to **London**, because London is the window this phase's strategy trades.

`preceding_session_window(as_of)` returns the window that most recently **ended at or before** `as_of`. At `07:00` on day D that is Asian `[D 00:00, D 07:00)`.

**Every function is pure and takes no bar store.** Timezone-naive input raises; use the existing `ensure_utc` from `core/clock.py`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, Session.ASIAN),
        (6, Session.ASIAN),
        (7, Session.LONDON),      # half-open: the boundary belongs to the later window
        (13, Session.LONDON),     # the London/New York overlap resolves to London
        (16, Session.NEW_YORK),
        (20, Session.NEW_YORK),
        (21, Session.OFF),
        (23, Session.OFF),
    ],
)
def test_the_session_of_a_moment(hour: int, expected: Session) -> None:
    assert session_of(datetime(2026, 9, 21, hour, 0, tzinfo=UTC)) is expected


def test_the_overlap_resolves_to_london_and_the_test_can_tell() -> None:
    """13:00 is inside both London (07-16) and New York (12-21).

    Asserted separately from the table because it is the one case where two
    windows are both correct answers and the tie-break is a decision, not
    arithmetic. If the order of the window table is reversed, this fails and
    the parametrized case above does too -- which is the point.
    """

    assert session_of(datetime(2026, 9, 21, 13, 0, tzinfo=UTC)) is Session.LONDON


def test_the_window_preceding_the_london_open_is_the_asian_session() -> None:
    session, start, end = preceding_session_window(datetime(2026, 9, 21, 7, 0, tzinfo=UTC))

    assert session is Session.ASIAN
    assert start == datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 21, 7, 0, tzinfo=UTC)


def test_a_naive_moment_is_refused() -> None:
    with pytest.raises(TimestampError):
        session_of(datetime(2026, 9, 21, 7, 0))
```

- [ ] **Step 2: Run them and watch them fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features/indicators/test_session.py -q --no-cov`
Expected: FAIL with `ModuleNotFoundError: trading_house.features.indicators.session`.

- [ ] **Step 3: Implement**

```python
"""Session windows, fixed in UTC.

Deliberately NOT timezone-aware. ``zoneinfo`` would make a backtest result
depend on the machine's tzdata version, and tzdata changes several times a
year -- so a phase whose contract is byte-reproducibility cannot define its
clock that way. The cost is that DST moves the effective local hour by one,
which the strategy spec states as a limitation rather than absorbing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from trading_house.core.clock import ensure_utc


class Session(str, Enum):  # noqa: UP042
    ASIAN = "asian"
    LONDON = "london"
    NEW_YORK = "new_york"
    OFF = "off"


# Order is load-bearing: London precedes New York so the 12:00-16:00 overlap
# resolves to London, which is the window this phase's strategy trades.
_WINDOWS: tuple[tuple[Session, int, int], ...] = (
    (Session.LONDON, 7, 16),
    (Session.NEW_YORK, 12, 21),
    (Session.ASIAN, 0, 7),
)


def session_of(moment: datetime) -> Session:
    """The window containing ``moment``. Half-open: 07:00 is London, not Asian."""

    hour = ensure_utc(moment).hour
    for session, start, end in _WINDOWS:
        if start <= hour < end:
            return session
    return Session.OFF


def session_bounds(moment: datetime, session: Session) -> tuple[datetime, datetime]:
    """The bounds of ``session`` on ``moment``'s UTC date."""

    if session is Session.OFF:
        raise ValueError("OFF is not a window with bounds")
    start_hour, end_hour = next((s, e) for name, s, e in _WINDOWS if name is session)
    midnight = ensure_utc(moment).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(hours=start_hour), midnight + timedelta(hours=end_hour)


def preceding_session_window(as_of: datetime) -> tuple[Session, datetime, datetime]:
    """The window that most recently ENDED at or before ``as_of``.

    At 07:00 that is the Asian window of the same date. Looks back across the
    date boundary, so the first London open after midnight resolves correctly.
    """

    moment = ensure_utc(as_of)
    candidates: list[tuple[Session, datetime, datetime]] = []
    for offset in (0, -1):
        day = moment + timedelta(days=offset)
        for session, _start, _end in _WINDOWS:
            start, end = session_bounds(day, session)
            if end <= moment:
                candidates.append((session, start, end))
    return max(candidates, key=lambda window: window[2])
```

- [ ] **Step 4: Run the tests**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features/indicators/test_session.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Mutation**

Reverse the first two entries of `_WINDOWS` so New York precedes London. Confirm the overlap test and the parametrized `13` case both fail, and nothing else does. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/features/indicators/session.py tests/unit/features/
git commit -m "feat(features): session windows, fixed in UTC"
```

---

### Task 4: Session features on the engine

**Files:**
- Modify: `src/trading_house/features/engine.py`
- Test: `tests/unit/features/test_engine.py`

**Interfaces:**
- Consumes: `Session`, `session_of`, `session_bounds`, `preceding_session_window` from Task 3.
- Produces, all on `FeatureEngine`:
  - `def prior_session_return(self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime) -> Decimal | None`
  - `def session_open_price(self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime) -> Decimal`
  - `def bars_since_session_open(self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime) -> int`

`FeatureEngine` remains the only holder of the bar store. Read the existing `atr` and `median_spread_points` for the house style: keyword-only options, `as_of` always last, and `_window`/`_read` for bar access.

**`prior_session_return` returns `None` when the preceding window holds no completed bars** — a holiday, a data gap, or the first window in the store. `None` is distinct from a return of zero, which is a real reading. Conflating them would hide a data problem inside a no-signal day (spec §4.2).

The return is close-to-close over the window: `(last.close - first.close) / first.close`.

- [ ] **Step 1: Write the failing tests**

```python
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
    able to tell them apart -- otherwise a feed outage looks like a flat night
    and the strategy silently stands down for the wrong reason."""

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

    assert (
        engine.prior_session_return(
            "fx.eurusd", Timeframe.M15, as_of=datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
        )
        == Decimal(0)
    )


def test_the_session_open_price_and_bar_count_at_the_open() -> None:
    engine = FeatureEngine(FakeBarReader(_asian_then_london()))
    as_of = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)

    assert engine.session_open_price("fx.eurusd", Timeframe.M15, as_of=as_of) == Decimal("1.10120")
    assert engine.bars_since_session_open("fx.eurusd", Timeframe.M15, as_of=as_of) == 0
```

Define `_asian_then_london()`, `_london_only()` and `_flat_asian_then_london()` as module-level helpers building M15 `Bar` sequences. Read `tests/unit/research/backtest/conftest.py`'s `_ramp` for the house style, and **make `FakeBarReader`'s bar list a public attribute** — a private one silently swallows a test's assignment, which produced a hollow test in an earlier phase.

- [ ] **Step 2: Run them and watch them fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features/test_engine.py -q --no-cov`
Expected: FAIL with `AttributeError: 'FeatureEngine' object has no attribute 'prior_session_return'`.

- [ ] **Step 3: Implement the three methods**

```python
    def prior_session_return(
        self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime
    ) -> Decimal | None:
        """Close-to-close return of the window that ended at or before ``as_of``.

        ``None`` when that window holds no completed bars, which is distinct
        from a return of zero: a strategy must be able to tell a feed outage
        from a flat night, or it stands down for the wrong reason.
        """

        _session, start, end = preceding_session_window(as_of)
        bars = [bar for bar in self._read(instrument_id, timeframe, as_of=as_of) if start <= bar.event_time < end]
        if not bars:
            return None
        return (bars[-1].close - bars[0].close) / bars[0].close

    def session_open_price(
        self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime
    ) -> Decimal:
        """Open of the first bar of the window containing ``as_of``."""

        return self._current_session_bars(instrument_id, timeframe, as_of=as_of)[0].open

    def bars_since_session_open(
        self, instrument_id: str, timeframe: Timeframe, *, as_of: datetime
    ) -> int:
        """Zero on the window's first closed bar."""

        return len(self._current_session_bars(instrument_id, timeframe, as_of=as_of)) - 1
```

Add one private helper returning the current window's bars up to `as_of`, raising a clear error if the window is empty — `session_open_price` has no sensible answer without a bar, and the strategy only asks inside a window it is already in.

- [ ] **Step 4: Run the tests**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/features/test_engine.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Mutation**

Change `if not bars: return None` to `return Decimal(0)`. Confirm the none-not-zero test fails and the flat-returns-zero test still passes — proving the two cases are genuinely distinguished rather than collapsed. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/features/engine.py tests/unit/features/
git commit -m "feat(features): session open, bar count and prior-session return"
```

---

### Task 5: Widen the snapshot

**Files:**
- Modify: `src/trading_house/research/backtest/snapshot.py`
- Modify: `src/trading_house/research/backtest/engine.py` (snapshot construction)
- Test: `tests/unit/research/backtest/test_snapshot.py`, `tests/unit/research/backtest/conftest.py`

**Interfaces:**
- Consumes: `Session` from Task 3; the three `FeatureEngine` methods from Task 4.
- Produces: `FeatureSnapshot` with four additional fields:
  - `session: Session`
  - `prior_session_return: Decimal | None`
  - `session_open_price: Decimal`
  - `bars_since_session_open: int` (`NonNegativeInt`)

The existing validator pinning `tick_spread_points` and `tick_time` to the closing bar stays. Add one more: **`session` must equal `session_of(bar.event_time)`** — a constructed invariant, so a caller cannot hand the strategy a snapshot whose session label disagrees with its own bar.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_snapshot_whose_session_disagrees_with_its_bar_is_refused() -> None:
    """The session label is derived from the bar, so it cannot be asserted
    independently of it. A caller that computes the session itself and gets it
    wrong would otherwise hand the strategy a snapshot that lies about when it
    is -- and the strategy's whole entry condition is a session test."""

    with pytest.raises(ValidationError):
        _snapshot(
            event_time=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),  # London
            session=Session.ASIAN,
        )


def test_a_snapshot_carries_the_session_features() -> None:
    snapshot = _snapshot(event_time=datetime(2026, 9, 21, 9, 0, tzinfo=UTC))

    assert snapshot.session is Session.LONDON
    assert snapshot.bars_since_session_open >= 0
```

- [ ] **Step 2: Run them and watch them fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest/test_snapshot.py -q --no-cov`
Expected: FAIL — `extra="forbid"` rejects the unknown `session` keyword.

- [ ] **Step 3: Add the fields and the validator**

```python
    session: Session
    prior_session_return: Decimal | None
    session_open_price: Decimal
    bars_since_session_open: NonNegativeInt
```

Extend the existing `model_validator` with:

```python
        if self.session is not session_of(self.bar.event_time):
            raise ValueError("session must equal the closing bar's own session")
```

- [ ] **Step 4: Fill the fields at the construction site**

In `engine.py`, the loop builds each `FeatureSnapshot`. Add the three `FeatureEngine` calls and `session_of(bar.event_time)`. Update `conftest.py`'s `_snapshot` helper to supply them with a `session` keyword that defaults to the bar's own session, so existing tests keep working and the new test can override it.

- [ ] **Step 5: Run the research suite**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research tests/unit/ops -q --no-cov`
Expected: PASS.

- [ ] **Step 6: Mutation**

Delete the new validator clause. Confirm the disagreement test fails and only it. Restore.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/research/backtest/ tests/unit/research/
git commit -m "feat(backtest): carry the session features on the snapshot"
```

---

### Task 6: Three gates that read the constitution

**Files:**
- Modify: `src/trading_house/risk/engine.py`
- Test: `tests/unit/risk/test_engine.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces three new `RejectionReason` members: `EDGE_BELOW_FLOOR = "edge_below_floor"`, `HOLDING_EXCEEDS_BOOK_LIMIT = "holding_exceeds_book_limit"`, `SWAP_EXCEEDS_EDGE_FRACTION = "swap_exceeds_edge_fraction"`.

**Background.** Until now the constitution's per-book limits layer was parsed, validated and read by nothing. `expected_return_bps`, `expected_cost_bps` and `win_probability` on `TradeProposal` were likewise consumed nowhere. These three gates are the first readers.

Which limit lives on which model matters: `min_expected_edge_after_cost_bps` is on **both** `ScalpLimits` and `SwingLimits` (amended at `247ba55`). `max_position_duration_seconds` is **scalp-only**. `max_swap_cost_pct_of_expected_edge` is **swing-only**. Use `isinstance` against the typed discriminated union rather than `getattr` or `hasattr` — the types are the documentation, and a `hasattr` check would silently skip a gate if a field were renamed.

The three gates join the existing `_record(...)` sequence of independent gates, each contributing its own reason, so a proposal failing two reports both.

The swap estimate: `expected_cost_bps` already includes the strategy's declared swap, so the gate needs the swap component separately. **Do not invent one.** Add `expected_swap_cost_bps: NonNegativeFiniteFloat` to `TradeProposal` with a default of `0.0`, so the strategy declares it and an intraday strategy declaring zero is making a statement the gate can check.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_proposal_below_the_books_edge_floor_is_refused() -> None:
    """fx_swing's floor is 2.0 bps after cost. A proposal declaring 5.0 of
    return against 4.0 of cost clears 1.0 bps, which is under it."""

    decision = _engine().evaluate(
        _proposal(book="fx_swing", expected_return_bps=5.0, expected_cost_bps=4.0),
        **_market(),
    )

    assert isinstance(decision, RejectedRiskDecision)
    assert RejectionReason.EDGE_BELOW_FLOOR in decision.reasons


def test_a_proposal_exactly_at_the_edge_floor_is_permitted() -> None:
    """The boundary belongs to the permitted side, and asserting it is what
    stops the comparison drifting between < and <=."""

    decision = _engine().evaluate(
        _proposal(book="fx_swing", expected_return_bps=6.0, expected_cost_bps=4.0),
        **_market(),
    )

    assert RejectionReason.EDGE_BELOW_FLOOR in decision.checks_passed


def test_a_scalp_proposal_holding_longer_than_its_book_permits_is_refused() -> None:
    """fx_scalp caps a position at 300 seconds."""

    decision = _engine().evaluate(
        _proposal(book="fx_scalp", max_holding_seconds=600), **_market()
    )

    assert isinstance(decision, RejectedRiskDecision)
    assert RejectionReason.HOLDING_EXCEEDS_BOOK_LIMIT in decision.reasons


def test_a_swing_proposal_whose_swap_eats_its_edge_is_refused() -> None:
    """fx_swing permits swap up to 20% of expected edge. 2.0 bps of swap
    against 6.0 of return and 4.0 of cost is 2.0 over an edge of 2.0 -- 100%."""

    decision = _engine().evaluate(
        _proposal(
            book="fx_swing",
            expected_return_bps=6.0,
            expected_cost_bps=4.0,
            expected_swap_cost_bps=2.0,
        ),
        **_market(),
    )

    assert isinstance(decision, RejectedRiskDecision)
    assert RejectionReason.SWAP_EXCEEDS_EDGE_FRACTION in decision.reasons


def test_the_duration_cap_does_not_apply_to_a_swing_book() -> None:
    """max_position_duration_seconds is declared on ScalpLimits only. A swing
    proposal holding nine hours is not refused by a limit its book does not
    have -- and a hasattr-style check would wrongly skip the scalp case too."""

    decision = _engine().evaluate(
        _proposal(book="fx_swing", max_holding_seconds=32400), **_market()
    )

    assert RejectionReason.HOLDING_EXCEEDS_BOOK_LIMIT not in decision.reasons
```

- [ ] **Step 2: Run them and watch them fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk/test_engine.py -q --no-cov -k "floor or holding or swap"`
Expected: FAIL with `AttributeError: EDGE_BELOW_FLOOR`.

- [ ] **Step 3: Add `expected_swap_cost_bps` to `TradeProposal`**

```python
    expected_swap_cost_bps: NonNegativeFiniteFloat = 0.0
```

Docstring it: the strategy's declared swap component, already included in `expected_cost_bps`, carried separately because `max_swap_cost_pct_of_expected_edge` needs it alone. A default of zero is a claim an intraday strategy is entitled to make and the gate can check.

- [ ] **Step 4: Add the three reasons and the three gates**

```python
    EDGE_BELOW_FLOOR = "edge_below_floor"
    HOLDING_EXCEEDS_BOOK_LIMIT = "holding_exceeds_book_limit"
    SWAP_EXCEEDS_EDGE_FRACTION = "swap_exceeds_edge_fraction"
```

Three predicates, joined to the existing `_record(...)` sequence:

```python
    @staticmethod
    def _edge_clears_floor(book: BookLimits, proposal: TradeProposal) -> bool:
        """Declared return minus declared cost, against the book's floor.

        The first gate in this system to read a proposal's declared economics.
        Until Phase 7 a strategy could claim any expected return and nothing
        looked -- which is why the numbers had no reason to be honest.
        """

        edge = Decimal(str(proposal.expected_return_bps)) - Decimal(
            str(proposal.expected_cost_bps)
        )
        return edge >= book.limits.min_expected_edge_after_cost_bps

    @staticmethod
    def _holding_within_book_limit(book: BookLimits, proposal: TradeProposal) -> bool:
        """Scalp books cap a position's duration; swing books do not declare one."""

        if not isinstance(book.limits, ScalpLimits):
            return True
        return proposal.max_holding_seconds <= book.limits.max_position_duration_seconds

    @staticmethod
    def _swap_within_edge_fraction(book: BookLimits, proposal: TradeProposal) -> bool:
        """Swing books cap swap as a percentage of expected edge."""

        if not isinstance(book.limits, SwingLimits):
            return True
        edge = Decimal(str(proposal.expected_return_bps)) - Decimal(
            str(proposal.expected_cost_bps)
        )
        if edge <= 0:
            return False
        swap = Decimal(str(proposal.expected_swap_cost_bps))
        return swap / edge * Decimal(100) <= book.limits.max_swap_cost_pct_of_expected_edge
```

Note the `Decimal(str(...))` conversions: the bps fields are `FiniteFloat` (analytics), the constitution's limits are `Decimal` (money-adjacent). Converting through `str` avoids binary-float artefacts in the comparison. Say so in a comment.

- [ ] **Step 5: Run the tests**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk -q --no-cov`
Expected: PASS.

- [ ] **Step 6: Mutation — three of them, one per gate**

Flip `>=` to `>` in `_edge_clears_floor`: confirm the exactly-at-the-floor test fails. Change `_holding_within_book_limit`'s `isinstance` guard to `return True` unconditionally: confirm the scalp duration test fails and the swing test still passes. Change `_swap_within_edge_fraction`'s `<=` to `<`: report what fails. Restore after each.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/risk/engine.py src/trading_house/core/schemas.py tests/unit/risk/
git commit -m "feat(risk): read the constitution's edge, duration and swap limits"
```

---

### Task 7: Unblock the fixed target

**Files:**
- Modify: `src/trading_house/core/schemas.py`
- Modify: `src/trading_house/risk/engine.py`
- Test: `tests/unit/risk/test_engine.py`

**Interfaces:**
- Consumes: Task 6's changes to the same two files.
- Produces: `TradeProposal.target_r_multiple: Decimal | None = None`, and `ExecutableRiskDecision.take_profit_price` populated instead of always `None`.

**Background.** `resolve_exit` in `fills.py` already implements target fills on both sides, with the gap rule and the stop-before-target ordering, fully tested. It has never executed, because the risk engine hard-codes `take_profit_price = None`. This task is wiring.

The engine computes the target from **its own** stop distance, so every price level is computed in one place — the same principle Phase 6's D-3 enforces for sizing. The strategy declares an R multiple; the engine sets the price.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_proposal_declaring_an_r_multiple_gets_a_target_priced_off_the_engines_stop() -> None:
    """The engine already computed the stop distance. Pricing the target from
    the strategy's own idea of the stop would let the two disagree."""

    decision = _engine().evaluate_for_execution(
        _proposal(side=Side.BUY, target_r_multiple=Decimal(2)), **_market()
    )

    assert isinstance(decision, ExecutableRiskDecision)
    distance = decision.entry_reference_price - decision.stop_loss_price
    assert decision.take_profit_price == decision.entry_reference_price + distance * 2


def test_a_sell_target_sits_below_the_entry() -> None:
    decision = _engine().evaluate_for_execution(
        _proposal(side=Side.SELL, target_r_multiple=Decimal(2)), **_market()
    )

    assert isinstance(decision, ExecutableRiskDecision)
    assert decision.take_profit_price < decision.entry_reference_price


def test_a_proposal_with_no_r_multiple_still_gets_no_target() -> None:
    """The baseline arm of the A/B has no target at all, so None must remain
    reachable -- otherwise the arm cannot be expressed."""

    decision = _engine().evaluate_for_execution(_proposal(target_r_multiple=None), **_market())

    assert isinstance(decision, ExecutableRiskDecision)
    assert decision.take_profit_price is None
```

Read `ExecutableRiskDecision` for the exact name of the field holding the entry reference; use that name rather than the one guessed here if they differ, and say so in your report.

- [ ] **Step 2: Run them and watch them fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk/test_engine.py -q --no-cov -k target`
Expected: FAIL — `extra="forbid"` rejects `target_r_multiple`.

- [ ] **Step 3: Add the field**

```python
    target_r_multiple: Decimal | None = None
```

Docstring: an R multiple, not a price. The engine prices it off the stop distance it computed, so the strategy cannot declare a target inconsistent with the stop actually applied.

- [ ] **Step 4: Compute the target in the decision assembly**

Replace `"take_profit_price": None` with a value computed from `stop_loss_price`, the entry reference and `proposal.target_r_multiple`, `None` when the multiple is `None`. Round to the contract's price grid with the same helper the stop uses — read how `stop_loss_price` is rounded and use that, so a target cannot land off-grid where a stop could not.

- [ ] **Step 5: Run the tests**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk tests/unit/research -q --no-cov`
Expected: PASS.

- [ ] **Step 6: Mutation**

Price the target off `proposal.invalidation_price` instead of the engine's `stop_loss_price`. Confirm the first test fails. This is the mistake the test exists to catch — the strategy's stop and the engine's are different numbers. Restore.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/core/schemas.py src/trading_house/risk/engine.py tests/unit/risk/
git commit -m "feat(risk): price a fixed target off the stop the engine computed"
```

---

### Task 8: `ExitPolicy` and a trailing stop in the simulator

**Files:**
- Modify: `src/trading_house/research/backtest/strategy.py`
- Modify: `src/trading_house/research/backtest/engine.py`
- Test: `tests/unit/research/backtest/test_engine.py`, `tests/property/test_trailing.py` (new)

**Interfaces:**
- Consumes: Task 2's corrected fill stamps; Task 7's target.
- Produces:
  - `class NoExitPolicy(CanonicalModel)` — `kind: Literal["none"]`
  - `class FixedTargetPolicy(CanonicalModel)` — `kind: Literal["fixed_target"]`, `r_multiple: Decimal`
  - `class ChandelierPolicy(CanonicalModel)` — `kind: Literal["chandelier"]`, `atr_multiple: Decimal`, `min_step_points: Decimal`
  - `ExitPolicy = Annotated[NoExitPolicy | FixedTargetPolicy | ChandelierPolicy, Field(discriminator="kind")]`
  - `Strategy.exit_policy() -> ExitPolicy` replacing `trail_policy() -> TrailPolicy | None`
  - `def trail_candidate(*, side: Side, current_stop: Decimal, bar: Bar, atr: Decimal, contract: InstrumentContract, policy: ChandelierPolicy) -> Decimal | None` in `engine.py`

**Rename, not addition.** `TrailPolicy` becomes `ExitPolicy` because a fixed target is not a trail. Phase 6's docstring reserves this widening.

**The three properties that make trailing not a toy** (master spec §9.3):
- **Monotonic (I-8).** A stop never moves away from profit. Returns `None` when the candidate does not improve.
- **Hysteresis.** A candidate closer to the current stop than `min_step_points` returns `None`.
- **Distance floor.** The candidate is clamped to the broker's minimum distance from the current price before the monotonic check, so the clamp cannot produce a stop that then moves backwards.

Order matters: clamp, then hysteresis, then monotonic. Write the order into the docstring and give it its own test.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_a_chandelier_stop_only_ever_moves_toward_profit() -> None:
    """I-8. A falling high must not drag a long's stop back down."""

    policy = ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal(3), min_step_points=Decimal(1)
    )
    high = _bar(high=Decimal("1.10500"))
    low = _bar(high=Decimal("1.10100"))

    raised = trail_candidate(
        side=Side.BUY, current_stop=Decimal("1.09900"), bar=high,
        atr=Decimal("0.00010"), contract=_contract(), policy=policy,
    )
    assert raised is not None and raised > Decimal("1.09900")

    assert trail_candidate(
        side=Side.BUY, current_stop=raised, bar=low,
        atr=Decimal("0.00010"), contract=_contract(), policy=policy,
    ) is None


def test_a_candidate_inside_the_minimum_step_is_ignored() -> None:
    """Without hysteresis the stop is re-modified on every bar, which in live
    trading is a request per tick to the broker."""

    policy = ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal(3), min_step_points=Decimal(100)
    )

    assert trail_candidate(
        side=Side.BUY, current_stop=Decimal("1.10190"), bar=_bar(high=Decimal("1.10500")),
        atr=Decimal("0.00010"), contract=_contract(), policy=policy,
    ) is None


def test_a_candidate_closer_than_the_brokers_minimum_distance_is_clamped() -> None:
    """A stop inside the freeze distance is rejected by MT5 outright, so the
    simulator must not produce one or it reports fills the venue would refuse."""

    policy = ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal("0.01"), min_step_points=Decimal(1)
    )
    bar = _bar(high=Decimal("1.10500"), close=Decimal("1.10500"))

    candidate = trail_candidate(
        side=Side.BUY, current_stop=Decimal("1.09900"), bar=bar,
        atr=Decimal("0.00010"), contract=_contract(), policy=policy,
    )

    assert candidate is not None
    assert bar.close - candidate >= _contract().stop_level_points * _contract().point_size
```

Read `InstrumentContract` for the actual field naming the minimum stop distance and use it; if the repo already has a `min_stop_distance` helper, reuse it rather than recomputing — say which you used.

- [ ] **Step 2: Write the Hypothesis property test**

```python
@given(
    highs=st.lists(
        st.decimals(min_value=Decimal("1.0"), max_value=Decimal("2.0"), places=5),
        min_size=2, max_size=40,
    )
)
def test_a_long_trail_is_monotonic_over_any_sequence_of_bars(highs: list[Decimal]) -> None:
    """I-8 over arbitrary price paths, not chosen examples.

    Monotonicity is exactly the shape Hypothesis falsifies well: a hand-written
    case proves the path it walks, and the paths that break a trail are the
    ones nobody thinks to write.
    """

    policy = ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal(3), min_step_points=Decimal(1)
    )
    stop = Decimal("0.50000")

    for high in highs:
        candidate = trail_candidate(
            side=Side.BUY, current_stop=stop, bar=_bar(high=high, close=high),
            atr=Decimal("0.00010"), contract=_contract(), policy=policy,
        )
        if candidate is not None:
            assert candidate > stop
            stop = candidate
```

- [ ] **Step 3: Run both and watch them fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest/test_engine.py tests/property/test_trailing.py -q --no-cov -k "trail or chandelier"`
Expected: FAIL with `ImportError` / `NameError` for `ChandelierPolicy` and `trail_candidate`.

- [ ] **Step 4: Rename `TrailPolicy` to `ExitPolicy` and add the variants**

Three `CanonicalModel`s and the discriminated union, as in the Interfaces block. Update `Strategy` to declare `exit_policy() -> ExitPolicy`. Update the toy in `ops/backtest.py` to return `NoExitPolicy(kind="none")` so the suite stays green until Task 11 replaces it.

- [ ] **Step 5: Implement `trail_candidate`**

Clamp first, then hysteresis, then monotonic — in that order, with a docstring saying why: clamping after the monotonic check could produce a stop that moves backwards.

- [ ] **Step 6: Wire it into the loop and add the target branch**

In `_close_if_done`, the open position's stop is updated from `trail_candidate` before the exit checks, and `resolve_exit` receives the strategy's target when the policy is `fixed_target`. Keep the existing order: the stop is asked before the target, and the target before the time stop.

- [ ] **Step 7: Run everything**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit tests/acceptance tests/property -q --no-cov`
Expected: PASS.

- [ ] **Step 8: Mutation**

Remove the monotonic check so any clamped candidate is returned. Confirm both the unit monotonic test and the Hypothesis property fail, and report the counterexample Hypothesis prints — a shrunk falsifying path is more useful evidence than a pass. Restore.

- [ ] **Step 9: Commit**

```bash
git add src/trading_house/research/backtest/ src/trading_house/ops/backtest.py tests/
git commit -m "feat(backtest): an exit policy with a target and a monotonic trail"
```

---

### Task 9: A declared tolerance for defective bars

**Files:**
- Modify: `src/trading_house/research/backtest/engine.py`
- Modify: `src/trading_house/research/backtest/result.py`
- Test: `tests/unit/research/backtest/test_engine.py`, `tests/unit/research/backtest/test_result.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `BacktestRequest.defective_bar_tolerance: Decimal` (a fraction in `[0, 1]`), and `BacktestResult.defective_bars: NonNegativeInt`.

**Why.** Phase 6 refuses a whole run if any bar is not `BarQuality.OK`, because dropping one silently leaves the run shorter than the period it claims. On two years of real EURUSD M15 that will fire. The tempting response — choosing ranges that happen to be clean — is selection on the data, and worse than the problem.

A declared tolerance keeps the honesty: the run may be shorter than it claims, but it must say by exactly how much, in the result.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_defective_bar_within_tolerance_is_counted_and_skipped() -> None:
    bars = _ramp(40)
    bars = (*bars[:20], bars[20].model_copy(update={"quality": BarQuality.SUSPECT}), *bars[21:])

    result = _run(bars=bars, strategy=ToyStrategy(), defective_bar_tolerance=Decimal("0.1"))

    assert result.defective_bars == 1
    assert result.bars_seen == 39


def test_a_defective_fraction_above_tolerance_still_refuses() -> None:
    """The tolerance is a declared allowance, not a way to ignore bad data."""

    bars = tuple(bar.model_copy(update={"quality": BarQuality.SUSPECT}) for bar in _ramp(40))

    with pytest.raises(BacktestRefused) as caught:
        _run(bars=bars, strategy=ToyStrategy(), defective_bar_tolerance=Decimal("0.1"))

    assert caught.value.kind is RefusalKind.DEFECTIVE_BAR


def test_the_default_tolerance_is_zero_so_phase_sixs_behaviour_is_unchanged() -> None:
    """A caller who says nothing gets the strict refusal, so the tolerance
    cannot be acquired by accident."""

    bars = _ramp(40)
    bars = (*bars[:20], bars[20].model_copy(update={"quality": BarQuality.SUSPECT}), *bars[21:])

    with pytest.raises(BacktestRefused):
        _run(bars=bars, strategy=ToyStrategy())
```

- [ ] **Step 2: Run them and watch them fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest/test_engine.py -q --no-cov -k defective`
Expected: FAIL — `_run` takes no `defective_bar_tolerance`.

- [ ] **Step 3: Add the request field with validation**

`defective_bar_tolerance: Decimal = Decimal(0)`, refused outside `[0, 1]` in `__post_init__` beside the existing `firm_equity` check. Default zero: Phase 6's behaviour is what a silent caller gets.

- [ ] **Step 4: Add the result field and count the skips**

`defective_bars: NonNegativeInt = 0` on `BacktestResult`. In the replay, a non-OK bar increments the count and is skipped rather than fed to the strategy; the refusal fires when `count / total` exceeds the tolerance. Decide the refusal check's position — before the loop if the counts are known up front, otherwise as the loop proceeds — and say which and why.

- [ ] **Step 5: Run the research suite**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research tests/unit/ops -q --no-cov`
Expected: PASS.

- [ ] **Step 6: Mutation**

Change the comparison so the tolerance is inclusive of an over-limit fraction (`>` to `>=` or the reverse, whichever loosens it). Report which test fails. Then change the default from `Decimal(0)` to `Decimal("0.1")` and confirm the default-is-zero test fails — the default is the part most likely to be "improved" later.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/research/backtest/ tests/unit/research/
git commit -m "feat(backtest): a declared tolerance for defective bars"
```

---

### Task 10: The strategy, its spec artifact, and the registry

**Files:**
- Create: `src/trading_house/strategies/__init__.py`, `spec.py`, `registry.py`, `impl/__init__.py`, `impl/session_momentum.py`
- Create: `tests/unit/strategies/test_spec.py`, `tests/unit/strategies/test_session_momentum.py`
- Create: `tests/acceptance/test_phase7.py`
- Modify: `tests/acceptance/test_architecture.py`

**Interfaces:**
- Consumes: `Session` (Task 3), the widened `FeatureSnapshot` (Task 5), `expected_swap_cost_bps` and `target_r_multiple` (Tasks 6-7), `ExitPolicy` (Task 8).
- Produces:
  - `class StrategySpec(CanonicalModel)` with the twelve §10.3 fields
  - `def registered(strategy_id: str) -> Strategy` and `REGISTERED_STRATEGY_IDS: frozenset[str]`
  - `class SessionMomentum` implementing `Strategy`, with `SESSION_MOMENTUM_ID = "session_momentum_eurusd"`

**`StrategySpec`'s twelve fields** (§10.3, in order): `economic_rationale`, `universe`, `trading_horizon`, `entry_rule`, `exit_rule`, `cost_model_description`, `capacity_model`, `invalidation`, `regime_constraints`, `trail_decision`, `trial_count`, `versioning`. All `NonEmptyStr` except `universe` (`tuple[InstrumentId, ...]`, `min_length=1`) and `trial_count` (`PositiveInt`).

`trail_decision` is `NonEmptyStr` and starts as a statement that the A/B has not yet run — **not** an empty string. A spec that cannot say what its trail decision is must say that, because a blank field reads as an answer.

**The entry rule, to implement exactly** (spec §4.2): on the first closed bar whose `session` is `LONDON` and whose `bars_since_session_open` is `0`, if `prior_session_return` is neither `None` nor zero, propose a trade in the direction of its sign, with `max_holding_seconds` equal to the seconds from the bar's `event_time` to 16:00 UTC that day.

**Import boundary.** `strategies/` may import `core/`, `features/` and `risk/` only. In particular it may **not** import `research/` — so `FeatureSnapshot` and `ExitPolicy`, which live in `research/backtest/`, are a problem. **Resolve it by moving both to `core/`** as part of this task: `FeatureSnapshot` to `src/trading_house/core/snapshot.py` and the `ExitPolicy` union to `src/trading_house/core/exits.py`, re-exported from their old locations so nothing else breaks. Report this as the structural consequence it is; if you judge a different resolution better, say so with reasoning before implementing it.

- [ ] **Step 1: Write the failing spec tests**

```python
def test_a_spec_missing_any_mandatory_item_is_refused() -> None:
    """Section 10.3 lists twelve items. A strategy that cannot produce all
    twelve does not register -- which is the only thing that keeps the trial
    count from being the field everyone forgets."""

    complete = _spec_fields()

    for omitted in complete:
        with pytest.raises(ValidationError):
            StrategySpec(**{k: v for k, v in complete.items() if k != omitted})


def test_a_blank_trail_decision_is_refused() -> None:
    """An empty trail decision reads as "no trailing" rather than "not yet
    tested", and those are different claims."""

    with pytest.raises(ValidationError):
        StrategySpec(**{**_spec_fields(), "trail_decision": ""})
```

- [ ] **Step 2: Write the failing strategy tests**

```python
def test_a_proposal_is_made_at_the_london_open_in_the_direction_of_the_night() -> None:
    proposal = SessionMomentum().evaluate(
        _snapshot(
            event_time=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
            session=Session.LONDON,
            bars_since_session_open=0,
            prior_session_return=Decimal("0.0012"),
        )
    )

    assert proposal is not None
    assert proposal.side is Side.BUY
    assert proposal.max_holding_seconds == 9 * 3600


def test_a_negative_night_proposes_a_sell() -> None:
    proposal = SessionMomentum().evaluate(
        _snapshot(
            event_time=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
            session=Session.LONDON,
            bars_since_session_open=0,
            prior_session_return=Decimal("-0.0012"),
        )
    )

    assert proposal is not None and proposal.side is Side.SELL


@pytest.mark.parametrize(
    ("session", "bars_since", "prior"),
    [
        (Session.ASIAN, 0, Decimal("0.0012")),      # wrong session
        (Session.LONDON, 1, Decimal("0.0012")),     # not the open bar
        (Session.LONDON, 0, Decimal(0)),            # flat night: no direction
        (Session.LONDON, 0, None),                  # no data: not the same as flat
    ],
)
def test_no_proposal_outside_the_entry_condition(
    session: Session, bars_since: int, prior: Decimal | None
) -> None:
    assert (
        SessionMomentum().evaluate(
            _snapshot(
                event_time=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
                session=session,
                bars_since_session_open=bars_since,
                prior_session_return=prior,
            )
        )
        is None
    )


def test_evaluate_is_pure(snapshot_fixture: FeatureSnapshot) -> None:
    """Section 10.2 mandates this explicitly: the same snapshot must give the
    same answer, or the same code cannot run in backtest, paper and live."""

    strategy = SessionMomentum()

    first = strategy.evaluate(snapshot_fixture)
    second = strategy.evaluate(snapshot_fixture)

    assert first is not None and second is not None
    assert first.model_dump(exclude={"proposal_id"}) == second.model_dump(exclude={"proposal_id"})
```

If `proposal_id` is derived from the snapshot rather than random, drop the `exclude` and assert full equality — and say which it is, because a random id inside a "pure function" is a contradiction worth naming.

- [ ] **Step 3: Write the architecture arrow test**

In `tests/acceptance/test_architecture.py`, add `STRATEGIES_ROOT` and `STRATEGIES_ALLOWED = {core, features, risk, trading_house.strategies}`, mirroring the allowlist shape the `research/backtest/` arrow uses. Add a guard-the-guard case proving the detector fires on a forbidden import, and a not-empty case proving the module glob matched something.

- [ ] **Step 4: Run them all and watch them fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/strategies tests/acceptance -q --no-cov`
Expected: FAIL with `ModuleNotFoundError: trading_house.strategies`.

- [ ] **Step 5: Move `FeatureSnapshot` and `ExitPolicy` to `core/`**

`core/snapshot.py` and `core/exits.py`, re-exported from `research/backtest/snapshot.py` and `research/backtest/strategy.py` so no other import changes. Run the full suite after this move alone, before writing the strategy, so a failure is attributable to the move rather than to new code.

- [ ] **Step 6: Implement `StrategySpec`, the registry, and `SessionMomentum`**

The strategy's declared economics: `expected_return_bps`, `expected_return_stdev_bps`, `expected_cost_bps`, `win_probability` and `expected_swap_cost_bps` are **declared constants on the class with a comment naming them as priors awaiting the run**, not computed. They must clear `fx_swing`'s 2.0 bps floor or the strategy refuses its own trades — set them so the floor is cleared and say in the comment that the first run replaces them with measured values.

- [ ] **Step 7: Run everything**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit tests/acceptance tests/property -q --no-cov` then the three gates.
Expected: PASS, gates clean.

- [ ] **Step 8: Mutation**

Change the entry condition's `bars_since_session_open == 0` to `>= 0`. Confirm the not-the-open-bar case fails. Then make `prior_session_return is None` fall through to the zero branch and confirm the `None` case fails separately from the zero case — the two must not collapse. Restore.

- [ ] **Step 9: Commit**

```bash
git add src/trading_house/strategies/ src/trading_house/core/ tests/
git commit -m "feat(strategies): the session-momentum strategy and its mandatory spec"
```

---

### Task 11: Composition, the A/B runner, and the README

**Files:**
- Modify: `src/trading_house/ops/backtest.py`
- Modify: `src/trading_house/cli.py`
- Modify: `README.md`
- Test: `tests/unit/ops/test_backtest.py`, `tests/unit/test_cli.py`, `tests/acceptance/test_phase7.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `backtest run --strategy session_momentum_eurusd --exit-policy {none,fixed_target,chandelier}`, with the toy and its `--toy-every-n` removed.

**The A/B's three arms and their pre-declared parameters** (spec §9.1). These are the only values the CLI accepts; there is no sweep, and the parameters are not options:

| Arm | Parameter | Value |
|---|---|---|
| `none` | — | — |
| `fixed_target` | `r_multiple` | `1.0` |
| `chandelier` | `atr_multiple`, `min_step_points` | `3.0`, `10` |

**Making them non-options is the point.** A `--target-r-multiple` flag is a sweep waiting to happen, and every value swept is a trial that deflates the Sharpe. If a later phase needs to vary them it can add the flag and account for the trials then.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_registry_holds_the_real_strategy_and_not_the_toy() -> None:
    assert SESSION_MOMENTUM_ID in REGISTERED_STRATEGY_IDS
    assert "toy" not in REGISTERED_STRATEGY_IDS


@pytest.mark.parametrize("arm", ["none", "fixed_target", "chandelier"])
def test_each_ab_arm_runs_and_produces_a_result(arm: str) -> None:
    result = runner.invoke(cli.app, _backtest_args(tmp_path, **{"--exit-policy": arm}))

    assert result.exit_code == 0
    assert json.loads(result.stdout)["result"]["bars_seen"] > 0


def test_the_arms_parameters_are_not_command_line_options() -> None:
    """Section 9.2: three arms, three trials, no sweep. A flag for the target
    multiple is a sweep waiting to happen, and each swept value is a trial
    that deflates the Deflated Sharpe this evidence feeds."""

    help_text = runner.invoke(cli.app, ["backtest", "run", "--help"]).stdout

    assert "--target-r-multiple" not in help_text
    assert "--atr-multiple" not in help_text
    assert "--toy-every-n" not in help_text
```

- [ ] **Step 2: Run them and watch them fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/ops tests/unit/test_cli.py -q --no-cov -k "registry or arm"`
Expected: FAIL — the toy is still the whole registry.

- [ ] **Step 3: Replace the toy with the real registry**

Delete `ToyStrategy`, `TOY_STRATEGY_ID` and `build_strategy`'s toy branch from `ops/backtest.py`; wire `strategies.registry`. `build_backtester` still constructs the `ReplayClock` itself and hands the same instance to both consumers — **do not change that**; it is what makes the clock-sharing mistake structurally impossible.

- [ ] **Step 4: Add `--exit-policy` and remove `--toy-every-n`**

The option is an enum of the three arm names, required, no default — the same reasoning as the cost options: an arm silently defaulting is an arm nobody chose.

- [ ] **Step 5: Update the README**

Replace the Phase 6 toy-registry paragraphs with Phase 7's: the strategy and its rationale, the four session features, the fixed-UTC-windows decision and its DST limitation, the three gates, the A/B's three arms and why their parameters are not flags, the defective-bar tolerance, and the fact that a negative result is a completed phase. Retitle the document from "Phase 1 Read-Only MT5 Gateway" to Phase 7 and rewrite the opening paragraph, which still says the repository cannot trade.

Wrap fenced command blocks with real `\`-continuations and verify with `cat -A` — a previous phase found that something in the write path eats a trailing backslash, and the check is one command.

- [ ] **Step 6: Run the whole suite and every gate**

```
UV_SYSTEM_CERTS=1 uv run pytest tests/unit tests/acceptance tests/property -q --no-cov
UV_SYSTEM_CERTS=1 uv run pytest tests/integration -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
```

Expected: PASS throughout. The integration suite needs Docker and has taken anywhere from 50 s to 8083 s on this machine — budget generously and do not conclude it has hung.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/ops/backtest.py src/trading_house/cli.py README.md tests/
git commit -m "feat: run the real strategy, one arm per invocation"
```

---

## After the tasks: the evidence run

**Not a task in this plan.** It needs a live MT5 terminal on the demo account, which no subagent has. The controller or the operator performs it and records the result in the `StrategySpec`:

1. `trading-house data backfill --instrument fx.eurusd --timeframe M15 --from <earliest the broker serves>`. Record the actual span.
2. Run each of the three arms **once**. Record net P&L after costs, trade count, defective-bar fraction, and the ranking.
3. Update `StrategySpec.trail_decision` and `trial_count`, and commit.

Any rerun after seeing the numbers is a new trial and gets counted.

## Self-Review

**1. Spec coverage.** Every spec section maps to a task: §4 features and rule → Tasks 3, 4, 5, 10; §5 snapshot → Task 5; §6 package and boundary → Task 10; §7 gates and the amendment → Task 6 plus commit `247ba55`; §8 target and trail → Tasks 7, 8; §9 the A/B → Task 11; §10 data and the defective-bar tolerance → Task 9 plus the evidence run; §11 testing → distributed across every task's mutation step; D-9 → Task 2. **One gap found and filled:** Task 1 covers no spec section — it fixes a Phase 6 escape discovered while setting this phase up, and it goes first because every later task adds a datetime-bearing model that the broken guard would not have checked.

**2. Placeholder scan.** No "TBD", no "add error handling", no "similar to Task N". Three tasks deliberately ask the implementer to read a name off the code rather than trust this plan — `ExecutableRiskDecision`'s entry-reference field (Task 7), `InstrumentContract`'s minimum-distance field (Task 8), and whether `proposal_id` is derived or random (Task 10). Each says to report what was found, because a plan that guesses an identifier and is wrong sends an implementer to make the code match the guess.

**3. Type consistency.** `Session` (Task 3) is used identically in Tasks 4, 5 and 10. `prior_session_return` is `Decimal | None` everywhere it appears. `ExitPolicy` is the name from Task 8 onward, never `TrailPolicy`. `expected_swap_cost_bps` is introduced in Task 6 and consumed in Task 10. `defective_bar_tolerance` and `defective_bars` keep their names in Tasks 9 and 11. **One consistency problem found and resolved in Task 10:** `strategies/` may not import `research/`, but `FeatureSnapshot` and `ExitPolicy` live there — so Task 10 moves both to `core/` with re-exports, and says so rather than leaving an implementer to discover the boundary violation at import time.
