# Phase 8B1 — Mark-to-Market Equity and Canonical Daily Returns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the backtest engine emit a mark-to-market equity observation at every processed bar, seal that series as verifiable evidence, and derive the canonical daily UTC return series from it — the first bundle whose basis is `MARK_TO_MARKET`.

**Architecture:** `run()` returns a new `BacktestOutcome { result, equity }` rather than a bare `BacktestResult`, so nothing about the result model changes and no digest moves. The series carries its own validators, and three assertions on `BacktestOutcome` bind it to the trades it came from — the answer to the objection `result.py:91-99` raises about duplicated state. `backtest run` gains seven declared-provenance options and, with `--mark-to-market`, emits a complete `EvidenceBundle` that the existing 8A `research trial record` seals unchanged.

**Tech Stack:** Python 3.12, Pydantic v2 strict/frozen models, stdlib `datetime`/`decimal`/`zip`, Typer, pytest, Hypothesis, Ruff, strict mypy. No new dependency.

## Global Constraints

- Target Python `>=3.12,<3.13`.
- Use `CanonicalModel` semantics: `strict=True`, `frozen=True`, `extra="forbid"` (`core/values.py:13`).
- Use `Decimal` for money, prices, and rates. Every exact factor multiplies first and the single division runs last, on an already-exact numerator — the shape `engine.py:673-677` and `costs.py:110-120` use, and the reason two runs over identical inputs cannot differ in trailing zeros and so cannot differ in digest.
- Every canonical timestamp is timezone-aware UTC and serializes with a `Z` suffix. Use the private `_utc()` helper wrapping `ensure_utc` and converting `TimestampError` to `ValueError`, mirroring `research/evidence.py:67-72` and `research/trial_ledger.py` rather than importing a private name across modules.
- Pydantic model validators raise `ValueError` and never a `TradingHouseError`; a plain function raises the typed error. This is why the identity violations surface as `ValidationError` and the non-positive daily denominator surfaces as `EquityEvidenceError`.
- Use absolute imports everywhere. `BACKTEST_ALLOWED` (`tests/acceptance/test_architecture.py:72-80`) admits `trading_house.research.backtest` but **not** `trading_house.research`, so a module under `research/backtest/` may not import from `research/`.
- Do not change `BacktestResult`, `SimulatedTrade`, or `CostModel` in any way. Their digests are load-bearing: four pinned constants name artifacts that exist on no machine but the one that produced them (see Task 4).
- Do not add a dependency. NumPy stays absent; it arrives with 8C.
- Do not add a ledger event type, a migration, or a new CLI command group. `research trial record` and `research trial verify` are unchanged by this plan.
- Do not implement umbrella §6.3 cost attribution, §6.4 cost scenarios, §6.5 compounding, or capacity diagnostics. Those are 8B2 and 8B3.
- `backtest run` without `--mark-to-market` must emit a byte-identical artifact. The shape `research/legacy_import.py:180-197` reads — `{"status": "ok", "result": ..., "digest": ..., "margin_modelled": false}` — is a contract with the Phase 7 evidence.
- Run the focused test first, then the quality gates named in each task.

## File Map

### Create

- `src/trading_house/research/backtest/mark.py` — `DailyReturnPoint`, `EquityObservation`, `EquitySeries`, `BacktestOutcome`, `derive_daily_returns`, `MAX_EQUITY_OBSERVATIONS`.
- `tests/unit/research/backtest/test_mark.py` — series identity, flatness, and the §6.2 daily rules.
- `tests/property/test_mark.py` — Hypothesis invariants over generated marks and ranges.
- `tests/integration/research/test_backtest_evidence.py` — real-database and real-CLI round trips for `--mark-to-market`.
- `tests/acceptance/test_phase8b1.py` — the 8B1 acceptance gate, including the four pinned digest constants.

### Modify

- `src/trading_house/core/errors.py` — `ExitCode.EQUITY_EVIDENCE = 18` and `EquityEvidenceError`.
- `src/trading_house/research/backtest/engine.py` — `run()` returns `BacktestOutcome`; the loop emits one mark per processed bar.
- `src/trading_house/research/evidence.py` — re-export `DailyReturnPoint` from its new home.
- `src/trading_house/research/__init__.py` — re-export the new public names.
- `src/trading_house/ops/backtest.py` — bundle assembly.
- `src/trading_house/cli.py` — seven options on `backtest run`, the `EXIT_CODES` entry, the identity guard.
- `tests/unit/research/backtest/test_engine.py` — unwrap `.result`; extend the known-answer proof to the series.
- `tests/integration/research/test_backtest_determinism.py` — unwrap `.result`.
- `tests/property/test_schema_boundaries.py` — builders for the new datetime-bearing models.
- `tests/unit/test_cli.py` — the new command group contract and exit code.
- `README.md` — the mark-to-market run, the seven options, and what a mid mark does and does not prove.

### Explicitly unchanged

- `src/trading_house/research/backtest/result.py` — not one field, not one docstring. Its "Do not add the field back" instruction stands; the sidecar is the answer to it.
- `src/trading_house/research/backtest/costs.py` — `stress_multiplier` is 8B2's business.
- `src/trading_house/research/legacy_import.py` — the legacy path and the three sealed v1 bundles are untouched.
- `migrations/` — no schema change in this slice.
- `pyproject.toml` — no dependency change.

---

### Task 1: The mark-to-market series and the daily return reduction

**Files:**
- Create: `src/trading_house/research/backtest/mark.py`
- Modify: `src/trading_house/core/errors.py`
- Modify: `src/trading_house/cli.py` (the `EXIT_CODES` entry only)
- Modify: `src/trading_house/research/evidence.py` (the `DailyReturnPoint` move)
- Modify: `tests/property/test_schema_boundaries.py` (the `EquityObservation` builder, or Task 1's naive-datetime sweep is vacuous for this phase's one new timestamp)
- Test: `tests/unit/research/backtest/test_mark.py`
- Test: `tests/property/test_mark.py`

**Interfaces:**
- Consumes: `CanonicalModel` (`core/values.py:13`), `ensure_utc` (`core/clock.py:8`), `TimestampError`, `BacktestResult` (`research/backtest/result.py:89`), pydantic's `NonNegativeInt`.
- Produces: `DailyReturnPoint`, `EquityObservation`, `EquitySeries`, `BacktestOutcome`, `derive_daily_returns(series, *, first_day, last_day) -> tuple[DailyReturnPoint, ...]`, `MAX_EQUITY_OBSERVATIONS`.
- Produces: `ExitCode.EQUITY_EVIDENCE = 18`, `EquityEvidenceError` in `core/errors.py`, and the `cli.EXIT_CODES` entry in the same change — `tests/unit/test_cli.py::test_every_typed_error_has_a_stable_exit_code` fails while a concrete error type is unmapped.
- `DailyReturnPoint` **moves** here from `research/evidence.py:80-86`, same shape. Task 1 also removes it from `evidence.py`; Task 3 re-exports it.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/research/backtest/test_mark.py`. `_FIRM = Decimal("100000")` and `_NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)`.

```python
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.core.errors import EquityEvidenceError
from trading_house.research.backtest.mark import (
    BacktestOutcome,
    EquityObservation,
    EquitySeries,
    derive_daily_returns,
)

_FIRM = Decimal("100000")
_NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)


def _point(
    *, minutes: int = 0, realized: Decimal = Decimal(0), unrealized: Decimal = Decimal(0), open_: int = 0
) -> EquityObservation:
    return EquityObservation(
        marked_at=_NOW + timedelta(minutes=minutes),
        equity=_FIRM + realized + unrealized,
        cumulative_realized_pnl=realized,
        unrealized_pnl=unrealized,
        open_positions=open_,
    )


def _series(*points: EquityObservation) -> EquitySeries:
    return EquitySeries(firm_equity=_FIRM, observations=points or (_point(),))


def test_every_point_must_satisfy_the_mark_identity() -> None:
    good = _point(realized=Decimal("10"), unrealized=Decimal("-4"))
    assert good.equity == _FIRM + Decimal("6")

    with pytest.raises(ValidationError, match="firm equity"):
        EquitySeries(firm_equity=_FIRM, observations=(good.model_copy(update={"equity": Decimal("999")}),))


def test_observations_must_move_forward_and_never_repeat() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        _series(_point(minutes=5), _point(minutes=5))

    with pytest.raises(ValidationError, match="strictly increasing"):
        _series(_point(minutes=5), _point(minutes=1))


def test_an_empty_series_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least one observation"):
        _series()


def test_is_flat_reads_the_final_open_position_count_not_a_stored_flag() -> None:
    assert _series(_point(), _point(minutes=15, open_=1)).is_flat is False
    assert _series(_point(), _point(minutes=15)).is_flat is True


def test_daily_returns_are_rectangular_across_the_requested_range() -> None:
    # Two marks on the first and last day of the range, so the days between
    # them have no mark and must still appear. Building the marks from _NOW
    # would place them in 2026 and silently make every requested day a
    # carried-forward zero, which is rectangular for the wrong reason.
    series = _series(
        EquityObservation(
            marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
            equity=_FIRM + Decimal("100"),
            cumulative_realized_pnl=Decimal("100"),
            unrealized_pnl=Decimal(0),
            open_positions=0,
        ),
        EquityObservation(
            marked_at=datetime(2024, 1, 4, 21, 0, tzinfo=UTC),
            equity=_FIRM + Decimal("400"),
            cumulative_realized_pnl=Decimal("400"),
            unrealized_pnl=Decimal(0),
            open_positions=0,
        ),
    )

    points = derive_daily_returns(series, first_day=date(2024, 1, 1), last_day=date(2024, 1, 4))

    assert [point.day.isoformat() for point in points] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]
    # The interior days carry the prior close forward rather than being absent.
    assert points[1].value == Decimal(0)
    assert points[3].value == Decimal("300") / (_FIRM + Decimal("100"))


def test_a_day_with_no_mark_carries_the_prior_close_and_returns_zero() -> None:
    series = _series(
        EquityObservation(
            marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
            equity=_FIRM + Decimal("100"),
            cumulative_realized_pnl=Decimal("100"),
            unrealized_pnl=Decimal(0),
            open_positions=0,
        )
    )

    points = derive_daily_returns(series, first_day=date(2024, 1, 1), last_day=date(2024, 1, 3))

    assert points[0].value == Decimal("100") / _FIRM
    assert points[1].value == Decimal(0)
    assert points[2].value == Decimal(0)


def test_the_first_day_denominator_is_initial_firm_equity_and_later_ones_the_prior_close() -> None:
    series = _series(
        EquityObservation(
            marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
            equity=_FIRM * 2,
            cumulative_realized_pnl=_FIRM,
            unrealized_pnl=Decimal(0),
            open_positions=0,
        )
    )

    points = derive_daily_returns(series, first_day=date(2024, 1, 1), last_day=date(2024, 1, 2))

    assert points[0].value == Decimal(1)
    assert points[1].value == Decimal(0)


def test_a_non_positive_prior_close_is_refused_rather_than_defaulted() -> None:
    series = _series(
        EquityObservation(
            marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
            equity=Decimal(0),
            cumulative_realized_pnl=-_FIRM,
            unrealized_pnl=Decimal(0),
            open_positions=0,
        )
    )

    with pytest.raises(EquityEvidenceError):
        derive_daily_returns(series, first_day=date(2024, 1, 1), last_day=date(2024, 1, 2))
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run pytest tests/unit/research/backtest/test_mark.py -q --no-cov`

Expected: collection failure — `trading_house.research.backtest.mark` does not exist.

- [ ] **Step 3: Add the error and its exit code**

In `src/trading_house/core/errors.py`, add `EQUITY_EVIDENCE = 18` to the `ExitCode` enum after `EVIDENCE_INTEGRITY = 17` (18 is the next free value), and add the class after `EvidenceIntegrityError`:

```python
class EquityEvidenceError(TradingHouseError):
    """Raised when mark-to-market equity evidence cannot be produced honestly.

    One error for both ways that happens: a series that cannot satisfy its own
    identity, and a run whose observation count exceeds the storage ceiling. The
    caller has one remedy for each -- do not record this run as evidence -- and
    the count that broke the ceiling travels on the private cause rather than in
    the message, which stays as uninformative as every other code here.
    """

    public_message = "mark-to-market equity evidence is not trustworthy"
```

- [ ] **Step 4: Create `mark.py`**

Create `src/trading_house/research/backtest/mark.py`:

```python
"""Mark-to-market equity observations and the canonical daily return series.

Phase 8B1. The engine emits one observation per processed bar; this module owns
the shape those take, the arithmetic that must hold at every one of them, and the
rectangular daily reduction ``derive_daily_returns`` performs on them.

A mark is a **valuation**, not a liquidation value. An open position is marked
at the bar's mid close, so the series reports it worth more than closing it
would actually fetch, because the fill model charges half-spread and slippage
into the entry fill and charges neither into stop or target exits. Every
drawdown figure a later phase derives from this series is mark-to-market and
never realizable, and the basis field on the bundle is what says so.

``DailyReturnPoint`` is defined here rather than in ``research/evidence.py``
because ``BACKTEST_ALLOWED`` admits ``trading_house.research.backtest`` and not
``trading_house.research``: a module in this subtree may not import from the one
above it. ``research/evidence.py`` re-exports the name, and the serialized shape
is unchanged, so no digest moves.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Self

from pydantic import NonNegativeInt, field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import EquityEvidenceError, TimestampError
from trading_house.core.values import CanonicalModel
from trading_house.research.backtest.result import BacktestResult

MAX_EQUITY_OBSERVATIONS = 2_000_000
# ponytail: an O(1) guard on a count the loop already knows. It is here to fail
# with a number instead of filling the disk; raise it only if a real run needs
# more, and prefer a coarser timeframe to a larger budget. The four-year M15
# Phase 7 run produced 99,988 observations, so this sits ~20x above realistic.


def _utc(value: datetime) -> datetime:
    """``research/evidence.py``'s own timestamp helper, mirrored rather than
    imported. Private there, and importing a private name across modules is how
    a second copy of a rule becomes two rules."""

    try:
        return ensure_utc(value)
    except TimestampError as error:
        raise ValueError(str(error)) from error


class DailyReturnPoint(CanonicalModel):
    """One day's return. A ``date`` and not a timestamp: a daily series whose
    entries carry a time of day has to be truncated before anyone can compare
    two of them."""

    day: date
    value: Decimal


class EquityObservation(CanonicalModel):
    """The state of the book at one bar's close, after that bar's own fills and
    exits have been applied."""

    marked_at: datetime
    equity: Decimal
    cumulative_realized_pnl: Decimal
    unrealized_pnl: Decimal
    open_positions: NonNegativeInt

    @field_validator("marked_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class EquitySeries(CanonicalModel):
    """Every processed bar's observation, over a fixed capital base.

    Validated against itself alone: the three assertions that need the trades
    live on ``BacktestOutcome``, which is the only type that holds both.
    """

    firm_equity: Decimal
    observations: tuple[EquityObservation, ...]

    @model_validator(mode="after")
    def time_only_moves_forward_and_every_point_reconciles(self) -> Self:
        if not self.observations:
            raise ValueError("an equity series needs at least one observation")
        # pairwise, not zip(series, series[1:]): the two are different lengths by
        # exactly one, so strict=True would raise on every real series.
        for earlier, later in pairwise(self.observations):
            if later.marked_at <= earlier.marked_at:
                raise ValueError("equity observations must be strictly increasing in UTC time")
        for point in self.observations:
            reconciled = self.firm_equity + point.cumulative_realized_pnl + point.unrealized_pnl
            if point.equity != reconciled:
                raise ValueError("equity must equal firm equity plus realized plus unrealized")
        return self

    @property
    def is_flat(self) -> bool:
        """Whether the run ended with nothing open.

        Derived rather than stored: a boolean field would duplicate
        ``open_positions`` on the final observation and could disagree with it.
        """
        return self.observations[-1].open_positions == 0


class BacktestOutcome(CanonicalModel):
    """What one backtest run produced: its reconciled result and the equity path
    that produced it.

    ``result.py`` refuses to carry an equity curve because, under D-4, the curve
    was exactly the running sum of ``net_pnl`` and storing it "would duplicate
    state that can disagree with the trades it was derived from". The first half
    of that stopped being true once an open position is marked. The second half
    is answered here rather than dismissed: the three assertions below are
    exactly the disagreement checks, and they fail closed.
    """

    result: BacktestResult
    equity: EquitySeries

    @model_validator(mode="after")
    def series_is_the_result_it_came_from(self) -> Self:
        if self.equity.firm_equity != self.result.firm_equity:
            raise ValueError("the series and the result must share one firm equity")
        if len(self.equity.observations) != self.result.bars_seen:
            raise ValueError("the series must hold one observation per processed bar")
        if self.equity.is_flat:
            final = self.equity.observations[-1].cumulative_realized_pnl
            if final != self.result.net_pnl:
                raise ValueError("a flat run's final realized total must equal the result's net PnL")
        return self


def derive_daily_returns(
    series: EquitySeries, *, first_day: date, last_day: date
) -> tuple[DailyReturnPoint, ...]:
    """The canonical daily return series: every UTC calendar day, inclusive.

    End-of-day equity is the final mark within that day. A day with no mark
    carries the prior end-of-day equity forward and therefore returns a literal
    zero -- a calendar-day series has no way to omit a day, and the bundle's
    ``return_series_basis`` is what stops a reader mistaking that zero for a
    measured one. The first day's denominator is initial firm equity; later ones
    are the preceding day's end equity and must be strictly positive.
    """

    closes: dict[date, Decimal] = {}
    for point in series.observations:
        closes[point.marked_at.date()] = point.equity

    points: list[DailyReturnPoint] = []
    previous_close = series.firm_equity
    day = first_day
    while day <= last_day:
        end_of_day = closes.get(day, previous_close)
        if previous_close <= 0:
            raise EquityEvidenceError()
        points.append(DailyReturnPoint(day=day, value=(end_of_day - previous_close) / previous_close))
        previous_close = end_of_day
        day += timedelta(days=1)
    return tuple(points)
```

Delete the `DailyReturnPoint` class from `src/trading_house/research/evidence.py` and import it instead, adding `DailyReturnPoint` to that module's imports from `research.backtest.mark` and to its `__all__`. Nothing else in `evidence.py` changes: the field type at `evidence.py:155` and the serialized shape are identical.

- [ ] **Step 5: Write the property tests**

Create `tests/property/test_mark.py`:

```python
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trading_house.core.errors import EquityEvidenceError
from trading_house.research.backtest.mark import (
    EquityObservation,
    EquitySeries,
    derive_daily_returns,
)

_FIRM = Decimal("100000")
_BASE = datetime(2024, 1, 1, tzinfo=UTC)
_MONEY = st.decimals(min_value=-1000000, max_value=1000000, places=2, allow_nan=False)


def _series_from(pairs: list[tuple[int, Decimal, Decimal]]) -> EquitySeries:
    return EquitySeries(
        firm_equity=_FIRM,
        observations=tuple(
            EquityObservation(
                marked_at=_BASE + timedelta(minutes=index),
                equity=_FIRM + realized + unrealized,
                cumulative_realized_pnl=realized,
                unrealized_pnl=unrealized,
                open_positions=0,
            )
            for index, realized, unrealized in pairs
        ),
    )


@given(st.lists(st.tuples(st.integers(0, 400), _MONEY, _MONEY), min_size=1, max_size=25))
@settings(max_examples=50)
def test_the_identity_holds_for_every_point_of_any_well_formed_series(pairs: list[tuple[int, Decimal, Decimal]]) -> None:
    series = _series_from([(index, realized, unrealized) for index, (_, realized, unrealized) in enumerate(pairs)])

    for point in series.observations:
        assert point.equity == _FIRM + point.cumulative_realized_pnl + point.unrealized_pnl


@given(
    st.integers(0, 200),
    st.integers(0, 200),
    st.lists(st.tuples(st.integers(0, 2000), _MONEY, _MONEY), min_size=1, max_size=20),
)
@settings(max_examples=50)
def test_the_daily_series_is_always_rectangular(
    offset: int, span: int, pairs: list[tuple[int, Decimal, Decimal]]
) -> None:
    series = _series_from([(index, realized, unrealized) for index, (_, realized, unrealized) in enumerate(pairs)])
    first = date(2024, 1, 1) + timedelta(days=offset)

    points = derive_daily_returns(series, first_day=first, last_day=first + timedelta(days=span))

    assert len(points) == span + 1
    assert [point.day for point in points] == [first + timedelta(days=n) for n in range(span + 1)]
```

The generated series keeps `unrealized_pnl` bounded well inside `firm_equity`, so `previous_close` stays positive and the identity property is not entangled with the refusal case. Add one more property asserting that a series whose closes are driven to zero raises `EquityEvidenceError` rather than returning a division result — construct it with a single observation whose realized P&L is exactly `-firm_equity` and a range of two days.

- [ ] **Step 6: Run the focused tests and the gates**

Run:
```powershell
uv run pytest tests/unit/research/backtest/test_mark.py tests/property/test_mark.py -q --no-cov
uv run ruff format src/trading_house/research/backtest/mark.py src/trading_house/core/errors.py tests/unit/research/backtest/test_mark.py tests/property/test_mark.py
uv run ruff check src/trading_house/research/backtest/mark.py src/trading_house/core/errors.py tests/unit/research/backtest/test_mark.py tests/property/test_mark.py
uv run mypy
```

Expected: all tests pass, Ruff clean, mypy reports no errors.

- [ ] **Step 7: Commit the series**

```powershell
git add src/trading_house/research/backtest/mark.py src/trading_house/core/errors.py src/trading_house/cli.py src/trading_house/research/evidence.py tests/property/test_schema_boundaries.py tests/unit/research/backtest/test_mark.py tests/property/test_mark.py
git commit -m "feat: define the mark-to-market series and the daily return reduction"
```

---

### Task 2: Emit one mark per processed bar

**Files:**
- Modify: `src/trading_house/research/backtest/engine.py`
- Modify: `src/trading_house/research/__init__.py`
- Modify: `tests/unit/research/backtest/conftest.py`
- Modify: `tests/unit/research/backtest/test_engine.py`
- Modify: `tests/integration/research/test_backtest_determinism.py`

**Interfaces:**
- Consumes: `BacktestOutcome`, `EquityObservation`, `EquitySeries`, `MAX_EQUITY_OBSERVATIONS` from Task 1.
- Produces: `BacktestEngine.run(request) -> BacktestOutcome` instead of `-> BacktestResult`. Every existing caller unwraps `.result`.

- [ ] **Step 1: Update the known-answer test and the existing open-position test**

`tests/unit/research/backtest/test_engine.py` uses no pytest fixtures. Every test builds its run
through the `_run(bars=..., strategy=...)` helper in
`tests/unit/research/backtest/conftest.py:406-457`, over `_ramp(n)` bars with a `ToyStrategy`.
Use those names; do not invent fixtures.

Extend the known-answer proof at `test_engine.py:40-90`, whose last assertion is
`result.net_pnl == Decimal("-40.44")`. Add to it, so a change to the emission point or the mark
formula fails a number rather than a shape:

```python
    assert result.bars_seen == 60
    # Phase 8B1: one mark per processed bar, and the run closed flat, so the
    # final observation reconciles to net PnL. Both trades were TIME exits, so
    # cumulative realized at the close is exactly the -40.44 above.
    assert len(outcome.equity.observations) == outcome.result.bars_seen
    assert outcome.equity.is_flat is True
    assert outcome.equity.observations[-1].cumulative_realized_pnl == Decimal("-40.44")
    assert outcome.equity.observations[-1].unrealized_pnl == Decimal(0)
    assert outcome.equity.observations[-1].equity == Decimal("100000") + Decimal("-40.44")
```

That test needs the whole outcome, so it calls the `_outcome` helper this task's Step 4 adds. Its
existing `result` local becomes `outcome.result`.

Now extend the test that already covers the open-position case,
`test_a_position_still_open_when_the_bars_run_out_produces_no_trade` at
`test_engine.py:262-281`. It already asserts `result.trades == ()`, `result.net_pnl == Decimal(0)`
and `result.bars_seen == 40` for `_ramp(40)` with a two-hour holding period. Add to it, because
this is precisely the run where the flat reconciliation must not apply:

```python
    # Phase 8B1: the discarded position is why the final observation is not
    # flat, and a not-flat series is why BacktestOutcome's reconciliation is
    # conditional. Without this the conditional could be vacuously satisfied.
    assert outcome.equity.is_flat is False
    assert outcome.equity.observations[-1].open_positions == 1
    assert len(outcome.equity.observations) == outcome.result.bars_seen
```

Add one new test for the emission point itself — that a processed bar which produced no proposal
still gets a mark, and that a bar the engine never looked at does not:

```python
def test_a_processed_bar_with_no_proposal_still_gets_a_mark() -> None:
    """One mark per bar the engine looked at, not per bar it traded.

    ``_ramp(60)`` gives 60 bars and the strategy asks for a proposal on every
    twentieth, so most bars are processed and untraded. The count is the whole
    claim: a mark per *trade* would understate the path, and a mark per raw bar
    would include bars the defective-bar check skipped.
    """

    outcome = _outcome(bars=_ramp(60), strategy=ToyStrategy(every_n=20))

    assert len(outcome.equity.observations) == 60
    assert outcome.result.bars_seen == 60
    assert len(outcome.result.trades) < 60
```


- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run pytest tests/unit/research/backtest/test_engine.py -q --no-cov`

Expected: failures — `engine.run(...)` returns a `BacktestResult`, which has no `.equity`.

- [ ] **Step 3: Change `run()` to return an outcome**

In `src/trading_house/research/backtest/engine.py`:

Change the signature at line 314:

```python
    def run(self, request: BacktestRequest) -> BacktestOutcome:
```

Add the import:

```python
from trading_house.research.backtest.mark import (
    MAX_EQUITY_OBSERVATIONS,
    BacktestOutcome,
    EquityObservation,
    EquitySeries,
)
```

Initialise the accumulator beside `bars_seen` at lines 345-346, and keep the running realized total:

```python
        bars_seen = 0
        snapshots_skipped = 0
        realized = Decimal(0)
        observations: list[EquityObservation] = []
```

Emit the mark inside the loop. It goes **after** the open and close handling at lines 353-360 and **before** `as_of = bar.availability_time` at line 362, so the observation is the state at this bar's close with this bar's own fills and any intrabar stop or target already applied:

```python
            # The mark is the state at this bar's close, so it is taken after
            # the open and the close have been applied and before the snapshot
            # that may skip the bar entirely. A bar that reached here was
            # processed -- it is already counted in ``bars_seen`` -- so it gets
            # a mark even when the strategy proposed nothing and the risk engine
            # rejected the idea. ``_gross_pnl`` is the same conversion the
            # eventual exit uses, with the bar's mid close standing in for an
            # exit price: a mark is a valuation, and pricing it through the
            # fill model would invent an exit that did not happen.
            if position is None:
                unrealized = Decimal(0)
            else:
                unrealized = self._gross_pnl(
                    side=position.signal.side,
                    entry_price=position.entry.price,
                    exit_price=bar.close,
                    lots=position.signal.lots,
                )
            if len(observations) >= MAX_EQUITY_OBSERVATIONS:
                raise EquityEvidenceError()
            observations.append(
                EquityObservation(
                    marked_at=bar.availability_time,
                    equity=request.firm_equity + realized + unrealized,
                    cumulative_realized_pnl=realized,
                    unrealized_pnl=unrealized,
                    open_positions=0 if position is None else 1,
                )
            )
```

Accumulate `realized` where a trade closes, in the block at lines 356-360:

```python
            if position is not None:
                closed = self._close_if_done(position, bar, request)
                if closed is not None:
                    trades.append(closed)
                    realized += closed.net_pnl
                    position = None
```

Change the return at lines 435-444 to build the outcome:

```python
        result = self._result(
            request,
            trades=tuple(trades),
            rejections=tuple(rejections),
            bars_seen=bars_seen,
            defective_bars=defective_bars,
            tolerance_fraction=tolerance_fraction,
            exit_policy=policy,
            snapshots_skipped=snapshots_skipped,
        )
        return BacktestOutcome(
            result=result,
            equity=EquitySeries(firm_equity=request.firm_equity, observations=tuple(observations)),
        )
```

Leave the comment at lines 430-434 in place. It is still exactly right: the position is still discarded, and the new conditional flat reconciliation in `BacktestOutcome` is what accommodates that rather than working around it.

Add `EquityEvidenceError` to the imports from `trading_house.core.errors`.
- [ ] **Step 4: Give the test helpers both views without touching 50 call sites**

`run()` now returns an outcome, and `tests/unit/research/backtest/test_engine.py` has 50 `_run(`
call sites that all want the result. Do not edit them. In
`tests/unit/research/backtest/conftest.py`, rename the existing body's function to `_outcome` and
return the whole outcome, then add a thin forwarder that keeps `_run`'s exact signature:

```python
def _outcome(
    *,
    bars: tuple[Bar, ...],
    strategy: ToyStrategy,
    firm_equity: Decimal = Decimal("100000"),
    start: datetime | None = None,
    end: datetime | None = None,
    defective_bar_tolerance: Decimal | None = None,
    contract: InstrumentContract | None = None,
    constitution_sha256: str | None = None,
    atr_period: int = ATR_PERIOD,
    spread_window: int = SPREAD_WINDOW,
    reader: FakeBarReader | None = None,
) -> BacktestOutcome:
    # ...the body currently in _run, unchanged, including the tolerance_kwargs
    # block, and now returning the outcome rather than the result...


def _run(**kwargs: Any) -> BacktestResult:
    """The result, for the many tests that only care about trades and their totals.

    Every one of the 50 call sites below wants the result and not the series, so
    this keeps them untouched. The series is reached through ``_outcome``, and
    the two cannot drift because one calls the other.
    """

    return _outcome(**kwargs).result
```

`_run`'s parameters stay explicit rather than becoming `**kwargs: Any`, so a mistyped keyword is
still a type error at the 50 call sites. Use `typing.Any` only on the forwarder.

Now find every other caller of `.run(` and unwrap:

```powershell
uv run rg -n "\.run\(" src/ tests/
```

`src/trading_house/ops/backtest.py` and `cli.py` take `.result`; the determinism test and the
Phase 6/7 acceptance suites take `.result`. Where a caller wants the digest, take
`outcome.result.digest()` — the digest is unchanged, because the series is not on the result.

Add to `src/trading_house/research/__init__.py`'s re-exports and its sorted, explicit `__all__`:
`BacktestOutcome`, `EquityObservation`, `EquitySeries`, `DailyReturnPoint`, `derive_daily_returns`,
`MAX_EQUITY_OBSERVATIONS`.

- [ ] **Step 5: Run the engine tests and the determinism integration test**

Run:
```powershell
uv run pytest tests/unit/research/backtest/ -q --no-cov
uv run pytest tests/integration/research/test_backtest_determinism.py -q --no-cov
uv run ruff format src/trading_house/research/backtest/engine.py tests/unit/research/backtest/test_engine.py
uv run ruff check src/trading_house/research/backtest/engine.py tests/unit/research/backtest/test_engine.py
uv run mypy
```

Expected: all pass. The determinism test passing unchanged is the load-bearing result: two runs over identical inputs still produce identical results **and** identical series, cross-process.

- [ ] **Step 6: Commit the emission**

```powershell
git add src/trading_house/research/backtest/engine.py src/trading_house/research/__init__.py tests/unit/research/backtest/test_engine.py tests/integration/research/test_backtest_determinism.py
git commit -m "feat: mark equity at every processed bar"
```

---

### Task 3: Emit a mark-to-market evidence bundle from `backtest run`

**Files:**
- Modify: `src/trading_house/ops/backtest.py`
- Modify: `src/trading_house/cli.py`
- Test: `tests/integration/research/test_backtest_evidence.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `BacktestOutcome` (Task 2), `derive_daily_returns` (Task 1), `EvidenceBundle` / `EvidenceProvenance` / `CostSummary` / `ReturnSeriesBasis` / `RegistrationState` / `HoldoutState` (8A), `EquityEvidenceError` (Task 1).
- Produces: `mark_to_market_bundle(...) -> EvidenceBundle` in `ops/backtest.py`; seven options on `backtest run`. The `EXIT_CODES` mapping for `EquityEvidenceError` is Task 1's, because the suite's typed-error guard requires the definition and its mapping to land together.

- [ ] **Step 1: Write the failing unit tests for the identity guard**

In `tests/unit/test_cli.py`, extend the command-group contract tuple with `"backtest"` if it is not already there, and add cases using the file's existing `_raiser` helper and stable exit codes:

```python
def test_equity_evidence_error_maps_to_its_own_exit_code() -> None:
    assert cli.EXIT_CODES[EquityEvidenceError] is cli.ExitCode.EQUITY_EVIDENCE
    assert int(cli.ExitCode.EQUITY_EVIDENCE) == 18
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/unit/test_cli.py -q --no-cov`

Expected: failure — `EquityEvidenceError` is not in `EXIT_CODES` and `ExitCode.EQUITY_EVIDENCE` does not exist.
- [ ] **Step 3: Map the error in `cli.py`**

`tests/unit/test_cli.py::test_every_typed_error_has_a_stable_exit_code` asserts every concrete
`TradingHouseError` subclass appears in `cli.EXIT_CODES`. A new error and its mapping must land in
the same change or the suite is red between tasks — so the mapping belongs here, not in Task 3.

Add `EquityEvidenceError` to the `trading_house.core.errors` import block in `src/trading_house/cli.py`
(it sorts before `EvidenceIntegrityError`, so let `ruff check --fix` place it) and add to `EXIT_CODES`
after the `EvidenceIntegrityError` line:

```python
    EquityEvidenceError: ExitCode.EQUITY_EVIDENCE,
```

In `src/trading_house/ops/backtest.py`, add the assembly. The CLI never builds a payload shape of
its own, the same rule `result_recorded_event` and `evidence_sealed_event` follow in `ops/ledger.py`:

```python
def mark_to_market_bundle(
    outcome: BacktestOutcome,
    *,
    trial_id: str,
    attempt_id: str,
    spec_sha256: str,
    agent_run_id: str,
    occurred_at: datetime,
    registered_at: datetime,
) -> EvidenceBundle:
    """The bundle `research trial record` seals for one mark-to-market run.

    ``costs`` is PARTIAL and never COMPLETE: 8B1 does not separate spread from
    slippage, because the fill model folds both into the entry price and
    discards the components. Writing a zero for either would turn an unmeasured
    term into a measured one, which is the substitution this framework exists
    to prevent.

    ``dataset_sha256`` is None rather than a digest of the bar store. 8B1 does
    not compute one, and an unavailable hash is the honest record; fabricating
    one from a query the store cannot reproduce is what the legacy importer
    refuses to do.

    ``source_artifact_sha256`` is the domain-separated canonical digest of the
    result, not of a file: `backtest run` builds this bundle in memory and
    writes no artifact, so there is no file to hash. It is a real digest of real
    bytes and it is *not* the same value as ``source_result_sha256``, which is
    the result's own declaration-ordered digest -- two different serialisations
    of the same model, deliberately.
    """

    result = outcome.result
    return EvidenceBundle(
        result_schema_version=1,
        trial_id=trial_id,
        attempt_id=attempt_id,
        spec_sha256=spec_sha256,
        source_result_sha256=result.digest(),
        result=result,
        daily_returns=derive_daily_returns(
            outcome.equity,
            first_day=result.start.date(),
            # Not ``result.end.date()``. The engine reads one bar past
            # ``request.end`` -- inclusive bar open times against a half-open
            # store range -- so the last mark can be stamped on the next UTC
            # day. Stopping at ``end.date()`` drops that bar's equity change
            # while ``net_pnl`` still counts its PnL, leaving a daily series
            # that does not reconcile with the result printed beside it.
            # ``legacy_import.py`` extends its walk for exactly this reason.
            last_day=max(
                result.end.date(),
                max(point.marked_at.date() for point in outcome.equity.observations),
            ),
        ),
        return_series_basis=ReturnSeriesBasis.MARK_TO_MARKET,
        costs=CostSummary(
            status=CostAttributionStatus.PARTIAL,
            commission=sum((trade.commission for trade in result.trades), Decimal(0)),
            swap=sum((trade.swap for trade in result.trades), Decimal(0)),
            spread_cost=None,
            slippage_cost=None,
        ),
        provenance=EvidenceProvenance(
            agent_run_id=agent_run_id,
            source_artifact_sha256=canonical_sha256(result),
            dataset_sha256=None,
            registered_at=registered_at,
            occurred_at=occurred_at,
            registration_state=RegistrationState.PROSPECTIVE,
            holdout_state=HoldoutState.NOT_DEFINED,
        ),
    )
```

- [ ] **Step 4: Add the seven options to `backtest run`**

In `backtest_run`, add seven options. `--mark-to-market` is a flag; the other six default to
`None`, because "required only when the flag is set" is not expressible in a Typer signature and is
checked in the body instead:

```python
    mark_to_market: Annotated[bool, typer.Option("--mark-to-market", help="Emit a sealable mark-to-market evidence bundle instead of the bare result artifact.")] = False,
    trial_id: Annotated[str | None, typer.Option("--trial-id", help="Declared candidate this run belongs to. Required with --mark-to-market.")] = None,
    attempt_id: Annotated[str | None, typer.Option("--attempt-id", help="Started attempt this run belongs to. Required with --mark-to-market.")] = None,
    spec_sha256: Annotated[str | None, typer.Option("--spec-sha256", help="Preregistered specification digest. Required with --mark-to-market.")] = None,
    agent_run_id: Annotated[str | None, typer.Option("--agent-run-id", help="Agent run that produced the candidate. Required with --mark-to-market.")] = None,
    occurred_at: Annotated[datetime | None, typer.Option("--occurred-at", help="The run's own UTC timestamp, as declared provenance.")] = None,
    registered_at: Annotated[datetime | None, typer.Option("--registered-at", help="When the attempt was registered, as declared provenance.")] = None,
```

Guard at the top of `operation()`:

```python
        identity = (trial_id, attempt_id, spec_sha256, agent_run_id, occurred_at, registered_at)
        supplied = [value for value in identity if value is not None]
        # Two directions, and the second is the one a one-sided ``!= all(...)``
        # misses: a *partial* set of identity options with no ``--mark-to-market``
        # would otherwise be dropped silently and the operator told nothing.
        if (mark_to_market and len(supplied) != len(identity)) or (
            not mark_to_market and supplied
        ):
            # An operator who left a flag off, or typed identity without asking
            # for a bundle, has made a mistake rather than produced evidence
            # that cannot be trusted -- so this is configuration, not an equity
            # failure. A bundle is only sealable if it names the attempt it
            # belongs to, and an identity nobody asked for is a flag typo.
            raise ConfigurationError()
```

Replace `build_backtester(...).run(request)` at lines 1245-1249 with:

```python
        outcome = build_backtester(
            bars=_bar_store(),
            contract=instrument_contract,
            constitution=loaded_constitution,
        ).run(request)
```

Then branch the payload. Without the flag the existing three keys are returned unchanged and
byte-identical:

```python
        if not mark_to_market:
            return {
                "result": cast(JsonValue, json.loads(outcome.result.model_dump_json())),
                "digest": outcome.result.digest(),
                "margin_modelled": False,
            }
        bundle = mark_to_market_bundle(
            outcome,
            trial_id=cast(str, trial_id),
            attempt_id=cast(str, attempt_id),
            spec_sha256=cast(str, spec_sha256),
            agent_run_id=cast(str, agent_run_id),
            occurred_at=cast(datetime, occurred_at),
            registered_at=cast(datetime, registered_at),
        )
        return {
            "bundle": cast(JsonValue, json.loads(bundle.model_dump_json())),
            "digest": canonical_sha256(bundle),
            "mark_to_market_flat": outcome.equity.is_flat,
        }
```

`mark_to_market_flat` is reported rather than enforced: a non-flat run is honest evidence that a
later gate will refuse, and hiding it here would only move the surprise.


- [ ] **Step 5: Write the integration tests**

Create `tests/integration/research/test_backtest_evidence.py`. Use the `CliRunner` and the fixtures `tests/integration/research/test_trial_cli.py` already establishes, and reuse a registered protocol and a started attempt so the round trip is a real one. Cover:

1. `backtest run --mark-to-market` emits a bundle whose `return_series_basis` is `MARK_TO_MARKET`, whose `daily_returns` is rectangular over the run's window, and whose `costs.status` is `PARTIAL` with both unknown components `None`.
2. `research trial record` seals that bundle and `research trial verify` reports a valid chain that re-reads the document.
3. Without `--mark-to-market` the payload has exactly the keys `result`, `digest`, `margin_modelled`, and the digest equals the same run's bundle-carrying `result.digest()`.
4. Omitting any one of the six identity options exits `ExitCode.CONFIGURATION` and writes nothing.
5. A run that ends with an open position exits 0 and reports `"mark_to_market_flat": false`, and its bundle still records and verifies.
6. A tampered observation — a bundle whose `equity.observations[0].equity` no longer satisfies the identity — is refused by `EvidenceStore.read` as `EvidenceIntegrityError` rather than parsing into a plausible series.

- [ ] **Step 6: Run the CLI tests and the gates**

Run:
```powershell
uv run pytest tests/unit/test_cli.py -q --no-cov
uv run pytest tests/integration/research/test_backtest_evidence.py -q --no-cov
uv run ruff format src/trading_house/cli.py src/trading_house/ops/backtest.py tests/integration/research/test_backtest_evidence.py
uv run ruff check src/trading_house/cli.py src/trading_house/ops/backtest.py tests/integration/research/test_backtest_evidence.py
uv run mypy
```

Expected: all pass. Case 3 is the one that matters most: it is the proof that the Phase 7 artifact contract survived.

- [ ] **Step 7: Commit the bundle surface**

```powershell
git add src/trading_house/cli.py src/trading_house/ops/backtest.py tests/unit/test_cli.py tests/integration/research/test_backtest_evidence.py
git commit -m "feat: emit a mark-to-market evidence bundle from backtest run"
```

---

### Task 4: Close the property, acceptance, and documentation gates

**Files:**
- Modify: `tests/property/test_schema_boundaries.py`
- Create: `tests/acceptance/test_phase8b1.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything Tasks 1-3 produced.
- Produces: the 8B1 acceptance gate, and the operator documentation for a series that is a valuation and not a liquidation value.

- [ ] **Step 1: Register the new datetime-bearing models**

`EquityObservation` declares `marked_at`, so `test_every_datetime_bearing_canonical_model_has_a_builder` (`tests/property/test_schema_boundaries.py:566-588`) fails until it is in `BUILDERS`. Add an entry following the `EvidenceProvenance` pattern at lines 457-469, with a comment saying why `STAMP` does not cover it — `marked_at` is not one of the four names in `STAMP`:

```python
    # STAMP does not reach this one: ``marked_at`` is a bar's close, not the
    # source/availability triple every other stamped model carries. Declared
    # explicitly so the naive-datetime sweep exercises its UTC validator.
    EquityObservation: {
        "marked_at": AWARE,
        "equity": Decimal("100000"),
        "cumulative_realized_pnl": Decimal("0"),
        "unrealized_pnl": Decimal("0"),
        "open_positions": 0,
    },
```

`EquitySeries` and `BacktestOutcome` hold no datetime of their own, so they need no entry — but if the sweep's `_declares_a_datetime_field` walks nested models rather than declared fields, add entries that nest the one above, resolved after the dict literal the way line 472 already does for `SimulatedTrade`.

Run: `uv run pytest tests/property/test_schema_boundaries.py -q --no-cov`

Expected: every strict/frozen/extra/UTC case passes, with no missing builder for any model this plan introduces.

- [ ] **Step 2: Write the acceptance gate**

Create `tests/acceptance/test_phase8b1.py` covering:

- `MARK_TO_MARKET` is now produced by a real command, closing the 8A gap where the enum member had no producer;
- the mark identity holds at every point of a real run's series, and the observation count equals `result.bars_seen`;
- **the four pinned digest constants are still exactly their literals** — the three Phase 7 result digests, and the bundle digest at `tests/property/test_trial_evidence.py:198`. State them as constants here and assert equality against the live values. This is the regression guard for the sidecar decision: if a future change puts the series on `BacktestResult`, this test fails and names itself;
- the three sealed v1 legacy bundles still verify, so the sidecar did not strand them;
- a flat run reconciles and a non-flat run is reported rather than refused;
- `backtest run` without `--mark-to-market` emits a payload with no `bundle` key.

- [ ] **Step 3: Update the README**

Add to the Phase 8A section (or a new `Phase 8B1` section directly after it):

- `backtest run --mark-to-market` and the six identity options, with one example invocation of each, in the operator command table already there;
- the fact that the series is a **mid-price valuation and not a liquidation value**, so a position marked at a bar close is worth more than closing it would fetch, and every later drawdown figure derived from it is mark-to-market and never realizable;
- that a mark immediately before an exit will not equal that exit's realized P&L, because the exit is priced from its trigger with exit-side costs the mark does not carry — the series is a valuation path, the trades are the accounting, and they are related without being the same claim;
- that `costs.status` is `PARTIAL` with spread and slippage `None` until 8B2 separates them, and that a zero is not a substitute for an unmeasured term;
- the `MAX_EQUITY_OBSERVATIONS` ceiling and its remedy: narrow the window or use a coarser timeframe; a run is never silently subsampled;
- the new exit code 18 in the operator and typed-error recovery tables;
- that 8B1 adds no statistics, no cost scenarios, and no compounding — those are 8B2, 8B3, and 8C.

- [ ] **Step 4: Run the acceptance, property, and architecture checks**

Run:
```powershell
uv run pytest tests/property/test_schema_boundaries.py tests/acceptance/ -q --no-cov
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

Expected: all pass, Ruff clean, mypy clean. `tests/acceptance/` includes `test_architecture.py`, which enforces that `mark.py` imports nothing outside `BACKTEST_ALLOWED` — if it fails, the import set is wrong, not the guard.

- [ ] **Step 5: Commit the gates and the documentation**

```powershell
git add tests/property/test_schema_boundaries.py tests/acceptance/test_phase8b1.py README.md
git commit -m "test: close the phase 8b1 acceptance and schema gates"
```

---

### Task 5: Verify the whole slice

**Files:**
- No source files are created.

**Interfaces:**
- Consumes: the completed 8B1 implementation.
- Produces: a verified repository state and a recorded operator flow.

- [ ] **Step 1: Run the complete non-container suite**

```powershell
$env:UV_SYSTEM_CERTS = "1"
uv run pytest -m "not integration" --no-cov
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv lock --check
git diff --check
git status --short
```

Expected: all non-integration tests pass, all gates pass, the lock is current, no whitespace errors, and nothing untracked.

- [ ] **Step 2: Run the complete container-backed suite**

```powershell
$env:UV_SYSTEM_CERTS = "1"
uv run pytest
```

Expected: the full suite passes at or above the configured 95% coverage floor. If Docker is unavailable, stop and report that the integration gate was not run; do not claim 8B1 complete.

- [ ] **Step 3: Drive the real operator flow**

With the local research database migrated and two DSNs exported as `README.md` documents:

```powershell
$env:TRADING_HOUSE_DATABASE_DSN = "postgresql://trading_house_runtime:development-only-runtime-password@127.0.0.1:5432/trading_house"
$env:TRADING_HOUSE_RESEARCH_LEDGER_DSN = "postgresql://trading_house_runtime:development-only-runtime-password@127.0.0.1:5432/trading_house_research"
$env:TRADING_HOUSE_EVIDENCE_ROOT = ".local/evidence"
```

Register a protocol, start one of its declared candidates, run the backtest with `--mark-to-market` into a file, record it, and verify. Expected: `register` reports the candidate count, `start` exits 0, `backtest run` emits a bundle whose `digest` is stable across a re-run over identical inputs, `record` seals it, and `verify` reports `valid: true`.

Then repeat `backtest run` without `--mark-to-market` and confirm the payload has no `bundle` key and the same `result.digest()` as the bundle's `source_result_sha256`.

- [ ] **Step 4: Check the final repository state**

```powershell
git status --short
git diff --stat
git diff --check
```

Expected: only intended source, test, and documentation files. No bundle JSON, no `.local/evidence` content, no DSN, and no contract file is staged.

- [ ] **Step 5: Commit only if verification changed a tracked file**

```powershell
git add README.md
git commit -m "docs: record the phase 8b1 mark-to-market flow"
```

If `README.md` did not change, do not create an empty commit.

---

## Plan Self-Review Checklist

- [x] Every 8B1 design section has a task: series and daily reduction (§6 of this plan, design §6), engine emission (design §5), bundle and CLI (design §8), fail-closed behaviour (design §7), guards and docs (design §9-10).
- [x] `BacktestResult`, `SimulatedTrade`, and `CostModel` are untouched, so all four pinned digest constants and the three sealed v1 bundles survive. Task 4 asserts it.
- [x] No task adds a dependency, a migration, a ledger event type, or a CLI command group.
- [x] The mark is emitted after the bar's fills and exits, so an observation means the state at that bar's close — and the count equals `bars_seen`, which is asserted in Tasks 2 and 4.
- [x] The flat reconciliation is conditional, and Task 2's open-position test proves the condition is real rather than trivially satisfied.
- [x] `DailyReturnPoint` moves to satisfy `BACKTEST_ALLOWED` without widening it, and its serialized shape is unchanged.
- [x] A non-flat run is reported, not refused, in both the CLI payload and the design.
- [x] Every new datetime-bearing model is registered in `BUILDERS` in the task that introduces it.
- [x] Every new CLI option has a failure-path test: the six-flag guard is Task 3's case 4.
- [x] The observation ceiling fails closed with a number and is never a silent subsample.
- [x] No step contains a placeholder; every code step shows the code, and every test step shows the test.
