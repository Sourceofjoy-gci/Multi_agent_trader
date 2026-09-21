# Phase 6 — The Backtester and Cost Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An event-driven simulator that replays stored bars in point-in-time order, puts a strategy's proposals through the real risk engine, simulates fills pessimistically, and produces a byte-reproducible result.

**Architecture:** A new `research/backtest/` package that acts as a composition root over `core/`, `marketdata/`, `features/` and `risk/`. The strategy interface is a pure function of a point-in-time snapshot; the fill model is pure functions over one bar; the engine is the only stateful piece. No strategy with an edge is built — correctness rests on toys whose P&L is computed by hand.

**Tech Stack:** Python 3.12, Pydantic v2 (strict, frozen, `extra="forbid"`), `Decimal` for all money and prices, `float` only for analytics, pytest, Hypothesis, uv, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-17-phase-6-backtester-design.md`

## Global Constraints

- Python 3.12. Pydantic v2 models are strict, frozen, `extra="forbid"`. **All money and prices are `Decimal`**; R-multiples and ratios are `FiniteFloat` — analytics, not money.
- mypy strict; Ruff select `["E","F","I","B","UP","SIM","RUF","S","PT"]`; line length 100. A `# noqa` for a rule that is not enabled fails RUF100.
- **Every `uv` command must be prefixed with `UV_SYSTEM_CERTS=1`** (TLS-inspecting proxy). `uv run mypy` takes **no path arguments** — the config sets `packages = ["trading_house"]`, and it does **not** check the test tree.
- **`research/backtest/` may import `core/`, `marketdata/`, `features/` and `risk/`. It may NOT import `brokers/` or `execution/`.** There is no venue in a backtest and no idempotent order ledger.
- **No credential, DSN, account number or raw broker message may appear in any error, log or result payload.**
- **D-1:** a strategy whose `horizon_seconds` is under **ten** bars of the run's timeframe is refused.
- **D-2:** on a bar whose range touches both stop and target, **the stop happened first, always**.
- **D-3:** the real `RiskEngine` sits in the decision path. It already calls `compute_stop_distance`, `stop_price` and `compute_volume` internally — **the simulator reads `approved_quantity` and `stop_loss_price` off the decision and never sizes anything itself.**
- **D-4:** constant notional equity per run.
- **D-5:** `CostModel` is a required input with **no default**. A zero-cost run must be impossible to obtain by omission.
- **D-6:** the simulator never gives a strategy a better price than it asked for, and always gives a worse one when the bar allows.
- **D-7:** one open position at a time.
- **D-8:** rejections are recorded, not dropped.

## Context the implementer needs

**Existing signatures this plan builds on, verified against source:**

```python
# trading_house.features.engine
class BarReader(Protocol):
    def bars(self, instrument_id: str, timeframe: Timeframe, *, start: datetime,
             end: datetime, as_of: datetime,
             include_defective: bool = False) -> tuple[Bar, ...]: ...
    def coverage(self, instrument_id: str, timeframe: Timeframe) -> Coverage: ...

class FeatureEngine:
    def __init__(self, store: BarReader) -> None: ...
    def atr(self, instrument_id: str, timeframe: Timeframe, *, period: int,
            as_of: datetime) -> Decimal: ...
    def median_spread_points(self, instrument_id: str, timeframe: Timeframe, *,
                             window: int, as_of: datetime) -> Decimal: ...

# trading_house.risk.engine
class MarginPort(Protocol):
    def free_margin(self) -> Decimal: ...
    def required_margin(self, *, instrument_id: str, side: Side,
                        quantity: Decimal, price: Decimal) -> Decimal: ...

class RiskEngine:
    def __init__(self, constitution: Constitution, clock: Clock) -> None: ...
    def evaluate_for_execution(self, proposal: TradeProposal, *, margin: MarginPort,
        contract: InstrumentContract, firm_equity: Decimal, atr: Decimal,
        median_spread_points: Decimal, tick_spread_points: Decimal,
        tick_time: datetime) -> RiskDecision: ...
```

`RiskDecision` is a discriminated union of `ApprovedRiskDecision`,
`ResizedRiskDecision` (both carrying `approved_quantity: PositiveQuantity`,
`stop_loss_price: Price`, `take_profit_price: Price | None`, `risk_money`) and
`RejectedRiskDecision` (carrying `reasons: tuple[str, ...]`).

`Bar` carries `event_time`, `availability_time`, `open`, `high`, `low`, `close`,
`tick_volume`, `spread: int`, `real_volume`, `quality: BarQuality`, and enforces
`availability_time > event_time`.

`InstrumentContract` carries `point_size`, `price_increment`,
`value_per_price_increment`, `quantity_increment`, `min_stop_distance`,
`freeze_distance`. **It has no commission and no swap rate** — those come from
`CostModel`.

`TradeProposal` extends `Stamped`, so its point-in-time field is
**`availability_time`**, not `as_of`. The spec's §8 shorthand "`proposal.as_of`"
means `proposal.availability_time`.

---

### Task 1: The snapshot and the strategy interface

**Files:**
- Create: `src/trading_house/research/backtest/__init__.py`
- Create: `src/trading_house/research/backtest/snapshot.py`
- Create: `src/trading_house/research/backtest/strategy.py`
- Test: `tests/unit/research/backtest/test_snapshot.py`

**Interfaces:**
- Consumes: `Bar`, `Timeframe`, `InstrumentContract`, `TradeProposal` from existing modules.
- Produces:
  ```python
  MIN_HORIZON_BARS: int = 10

  class FeatureSnapshot(CanonicalModel):
      as_of: datetime
      instrument_id: InstrumentId
      timeframe: Timeframe
      bar: Bar                      # the bar that just closed
      atr: Decimal
      median_spread_points: Decimal
      tick_spread_points: Decimal   # == the closing bar's own spread
      tick_time: datetime           # == the closing bar's availability_time

  class TrailPolicy(CanonicalModel):
      kind: Literal["none"]

  class Strategy(Protocol):
      id: str
      version: str
      book: BookId
      horizon_seconds: int
      max_holding_seconds: int
      def evaluate(self, snapshot: FeatureSnapshot) -> TradeProposal | None: ...
      def trail_policy(self) -> TrailPolicy | None: ...

  def horizon_is_simulatable(*, horizon_seconds: int, timeframe: Timeframe) -> bool
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/research/backtest/test_snapshot.py
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.snapshot import (
    MIN_HORIZON_BARS,
    FeatureSnapshot,
    horizon_is_simulatable,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def test_the_snapshot_ties_its_tick_fields_to_the_bar_that_closed() -> None:
    """tick_spread_points and tick_time are named for a tick stream this phase
    does not have. Their meaning is the closing bar's own spread and
    availability_time -- stated as a constructed invariant rather than left to
    whichever caller builds a snapshot next."""

    snapshot = _snapshot(spread=17)

    assert snapshot.tick_spread_points == Decimal(17)
    assert snapshot.tick_time == snapshot.bar.availability_time
    assert snapshot.tick_time == snapshot.as_of


def test_a_snapshot_whose_tick_fields_disagree_with_its_bar_is_refused() -> None:
    """Without this the two representations drift, and the risk engine's
    spread-blowout gate silently judges a different bar than the strategy saw."""

    with pytest.raises(ValueError, match="tick_spread_points"):
        _snapshot(spread=17, tick_spread_points=Decimal(3))


@pytest.mark.parametrize(
    ("horizon_seconds", "timeframe", "expected"),
    [
        (600, Timeframe.M1, True),    # 10 bars exactly -- the boundary is allowed
        (599, Timeframe.M1, False),   # one second under it
        (30, Timeframe.M1, False),    # the 30-second scalp D-1 exists to refuse
        (36_000, Timeframe.H1, True),
    ],
)
def test_a_horizon_under_ten_bars_is_not_simulatable(
    horizon_seconds: int, timeframe: Timeframe, expected: bool
) -> None:
    """D-1. Below roughly ten bars most trades open and close inside a couple of
    bars, so D-2's stop-first pessimism dominates and the number being measured
    is the simulator's convention rather than the strategy."""

    assert (
        horizon_is_simulatable(horizon_seconds=horizon_seconds, timeframe=timeframe)
        is expected
    )


def test_the_threshold_is_a_named_constant_not_a_literal() -> None:
    """A future phase that ingests ticks lowers this. A literal buried in a
    comparison is one nobody finds."""

    assert MIN_HORIZON_BARS == 10
```

Write `_snapshot(...)` as a local helper in that file building a `Bar` with
`quality=BarQuality.OK`, `event_time=NOW - timeframe duration`,
`availability_time=NOW`, and OHLC `Decimal("1.10000")`-ish values, defaulting
`tick_spread_points` and `tick_time` to the bar's own.

- [ ] **Step 2: Run them and watch them fail**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest/test_snapshot.py -q --no-cov
```
Expected: collection error, `ModuleNotFoundError: trading_house.research.backtest.snapshot`.

- [ ] **Step 3: Write `snapshot.py`**

`FeatureSnapshot` is a `CanonicalModel` with a `model_validator(mode="after")`
that raises `ValueError("tick_spread_points must equal the closing bar's spread")`
when `self.tick_spread_points != Decimal(self.bar.spread)`, and a matching check
tying `tick_time` to `bar.availability_time` and `as_of`.

`horizon_is_simulatable` is:

```python
def horizon_is_simulatable(*, horizon_seconds: int, timeframe: Timeframe) -> bool:
    """D-1. Ten bars is a judgement, not a derivation -- below roughly that,
    most trades open and close inside a couple of bars, so D-2's stop-first
    pessimism dominates and the number being measured is the simulator's
    convention rather than the strategy."""

    bar_seconds = duration(timeframe).total_seconds()
    return horizon_seconds >= MIN_HORIZON_BARS * bar_seconds
```

`duration` is `trading_house.marketdata.models.duration`, which already maps
every `Timeframe` to a `timedelta`. Do not add a second mapping.

- [ ] **Step 4: Write `strategy.py`**

`Strategy` is a `typing.Protocol` with the attributes and methods in the
Interfaces block. `TrailPolicy` has a single `kind: Literal["none"]` member:
Phase 7 widens it, and a policy that cannot express trailing is the honest shape
while §9.2's A/B evidence does not exist.

- [ ] **Step 5: Run to green, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/research/backtest tests/unit/research
git commit -m "feat: the backtest snapshot and strategy interface"
```

---

### Task 2: The cost model

**Files:**
- Create: `src/trading_house/research/backtest/costs.py`
- Test: `tests/unit/research/backtest/test_costs.py`

**Interfaces:**
- Consumes: `InstrumentContract`, `Side`.
- Produces:
  ```python
  class CostModel(CanonicalModel):
      commission_per_lot_per_side: Decimal      # account currency
      slippage_points_per_side: Decimal
      swap_long_points_per_day: Decimal         # signed; negative is a charge
      swap_short_points_per_day: Decimal
      triple_swap_weekday: int                  # 0=Monday .. 6=Sunday
      stress_multiplier: Decimal = Decimal(1)

  def commission_cost(*, model: CostModel, lots: Decimal) -> Decimal      # both sides
  def slippage_price_offset(*, model: CostModel, side: Side,
                            contract: InstrumentContract, opening: bool) -> Decimal
  def swap_cost(*, model: CostModel, side: Side, lots: Decimal,
                contract: InstrumentContract, opened_at: datetime,
                closed_at: datetime) -> Decimal
  ```

- [ ] **Step 1: Write the failing tests**

```python
def test_a_cost_model_cannot_be_constructed_without_stating_its_costs() -> None:
    """D-5. The classic flattering backtest is one that silently assumed zero
    commission. Omitting a field must be an error, not a zero."""

    with pytest.raises(ValidationError):
        CostModel(slippage_points_per_side=Decimal("0.5"))  # type: ignore[call-arg]


def test_commission_is_charged_on_both_sides() -> None:
    """A round trip pays twice. Charging once understates cost by half, which
    is exactly the size of error that turns a losing strategy into a winner."""

    model = _model(commission_per_lot_per_side=Decimal("3.50"))

    assert commission_cost(model=model, lots=Decimal("2")) == Decimal("14.00")


def test_slippage_always_moves_the_price_against_the_trade() -> None:
    """D-6, in the one place a sign error is invisible: a buy slips up on entry
    and down on exit, and a sell the other way. Getting one of the four wrong
    produces a simulator that pays slippage on three legs and earns it on one."""

    model = _model(slippage_points_per_side=Decimal("2"))
    contract = _contract(point_size=Decimal("0.00001"))

    buy_in = slippage_price_offset(model=model, side=Side.BUY, contract=contract, opening=True)
    buy_out = slippage_price_offset(model=model, side=Side.BUY, contract=contract, opening=False)
    sell_in = slippage_price_offset(model=model, side=Side.SELL, contract=contract, opening=True)
    sell_out = slippage_price_offset(model=model, side=Side.SELL, contract=contract, opening=False)

    assert buy_in == Decimal("0.00002")    # pay up to get in
    assert buy_out == Decimal("-0.00002")  # sell lower to get out
    assert sell_in == Decimal("-0.00002")
    assert sell_out == Decimal("0.00002")


def test_the_stress_multiplier_scales_every_term_together() -> None:
    """Section 11.2's gate is "profitable at 1.5x-2x expected costs". One
    multiplier over the whole model rather than a second code path -- a
    separate stressed path is one that drifts from the unstressed one."""

    base = _model(commission_per_lot_per_side=Decimal("3.00"))
    stressed = base.model_copy(update={"stress_multiplier": Decimal(2)})

    assert commission_cost(model=stressed, lots=Decimal(1)) == Decimal("12.00")
    assert commission_cost(model=base, lots=Decimal(1)) == Decimal("6.00")


def test_swap_is_tripled_on_the_rollover_weekday() -> None:
    """MT5 charges three days of swap on one weekday to cover the weekend. A
    position held across it pays three times, and a swing strategy held for days
    pays it repeatedly."""

    model = _model(swap_long_points_per_day=Decimal("-1"), triple_swap_weekday=2)
    contract = _contract(point_size=Decimal("0.00001"), value_per_price_increment=Decimal(1))
    # Monday 2026-09-21 to Thursday 2026-09-24: crosses Mon->Tue, Tue->Wed
    # (the triple), Wed->Thu. Five days of swap charged over three nights.
    cost = swap_cost(
        model=model, side=Side.BUY, lots=Decimal(1), contract=contract,
        opened_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        closed_at=datetime(2026, 9, 24, 9, 0, tzinfo=UTC),
    )

    assert cost == Decimal("-5")


def test_a_position_closed_the_same_day_pays_no_swap() -> None:
    """Swap accrues at the daily rollover. An intraday strategy never pays it,
    and charging it anyway would penalise exactly the horizons this phase can
    simulate best."""

    model = _model(swap_long_points_per_day=Decimal("-1"))
    cost = swap_cost(
        model=model, side=Side.BUY, lots=Decimal(1), contract=_contract(),
        opened_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        closed_at=datetime(2026, 9, 21, 17, 0, tzinfo=UTC),
    )

    assert cost == Decimal(0)
```

Write `_model(**overrides)` and `_contract(**overrides)` local helpers supplying
every required field, so each test states only what it is about.

- [ ] **Step 2: Run them and watch them fail.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `costs.py`**

`stress_multiplier` multiplies inside every function, not at one call site — a
caller that forgets to apply it is the failure this shape removes.

Swap counts **rollover crossings**, not elapsed days: the number of dates
strictly between `opened_at.date()` and `closed_at.date()` inclusive of the
close date and exclusive of the open date, with the `triple_swap_weekday`
crossing counting three. Put that rule in the docstring — it is the part a
reader will otherwise re-derive wrongly.

- [ ] **Step 4: Prove the multiplier is not bypassable**

Delete the `stress_multiplier` factor from `commission_cost` only, confirm
`test_the_stress_multiplier_scales_every_term_together` fails and the other
tests pass, then restore. Quote the output in your report.

- [ ] **Step 5: Run to green, gates, commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/research/backtest/costs.py tests/unit/research/backtest/test_costs.py
git commit -m "feat: the backtest cost model and its stress multiplier"
```

---

### Task 3: The fill model

**Files:**
- Create: `src/trading_house/research/backtest/fills.py`
- Test: `tests/unit/research/backtest/test_fills.py`

**Interfaces:**
- Consumes: `Bar`, `InstrumentContract`, `Side`, `CostModel`, `slippage_price_offset`.
- Produces:
  ```python
  class ExitKind(str, Enum):
      STOP = "stop"
      TARGET = "target"
      TIME = "time"

  @dataclass(frozen=True, slots=True)
  class Fill:
      price: Decimal
      at: datetime

  @dataclass(frozen=True, slots=True)
  class Exit:
      kind: ExitKind
      fill: Fill

  def entry_fill(*, bar: Bar, side: Side, contract: InstrumentContract,
                 model: CostModel) -> Fill

  def resolve_exit(*, bar: Bar, side: Side, stop: Decimal, target: Decimal | None,
                   contract: InstrumentContract, model: CostModel) -> Exit | None
  ```

- [ ] **Step 1: Write the failing tests**

```python
def test_an_entry_fills_at_the_next_bars_open_plus_half_the_spread() -> None:
    """Section 11.1's named violation is executing at the same close that
    generated the signal. The bar passed here is already the NEXT one; the
    spread is that bar's own, converted through point_size."""

    bar = _bar(open=Decimal("1.10000"), spread=20)
    fill = entry_fill(bar=bar, side=Side.BUY, contract=_contract(), model=_zero_slip())

    assert fill.price == Decimal("1.10010")   # half of 20 points at 0.00001
    assert fill.at == bar.availability_time


def test_a_stop_gapped_through_fills_at_the_open_not_the_stop() -> None:
    """The single most common lie in bar backtesting. Price gapped past the
    stop overnight; filling AT the stop invents liquidity that never existed,
    and it flatters exactly the trades that hurt most."""

    bar = _bar(open=Decimal("1.09000"), high=Decimal("1.09100"), low=Decimal("1.08900"))
    exit_ = resolve_exit(
        bar=bar, side=Side.BUY, stop=Decimal("1.09700"), target=None,
        contract=_contract(), model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.kind is ExitKind.STOP
    assert exit_.fill.price == Decimal("1.09000")   # the open, NOT 1.09700


def test_a_stop_touched_within_the_bar_fills_at_the_stop() -> None:
    """The ordinary case: the bar opened above the stop and traded down through
    it, so the stop is the honest fill."""

    bar = _bar(open=Decimal("1.10000"), high=Decimal("1.10100"), low=Decimal("1.09650"))
    exit_ = resolve_exit(
        bar=bar, side=Side.BUY, stop=Decimal("1.09700"), target=None,
        contract=_contract(), model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.fill.price == Decimal("1.09700")


def test_a_target_gapped_past_fills_at_the_target_never_better() -> None:
    """D-6's other half. A favourable gap is where an optimistic simulator
    hands the strategy money the market never offered."""

    bar = _bar(open=Decimal("1.11000"), high=Decimal("1.11200"), low=Decimal("1.10900"))
    exit_ = resolve_exit(
        bar=bar, side=Side.BUY, stop=Decimal("1.09700"), target=Decimal("1.10500"),
        contract=_contract(), model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.kind is ExitKind.TARGET
    assert exit_.fill.price == Decimal("1.10500")   # the target, NOT 1.11000


def test_a_bar_touching_both_resolves_to_the_stop() -> None:
    """D-2. The bar cannot say which came first. Assuming the target lets a
    losing strategy look profitable indefinitely; assuming the stop costs some
    genuine winners, which is the recoverable direction to be wrong in."""

    bar = _bar(open=Decimal("1.10000"), high=Decimal("1.10600"), low=Decimal("1.09650"))
    exit_ = resolve_exit(
        bar=bar, side=Side.BUY, stop=Decimal("1.09700"), target=Decimal("1.10500"),
        contract=_contract(), model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.kind is ExitKind.STOP


def test_a_bar_touching_neither_leaves_the_position_open() -> None:
    bar = _bar(open=Decimal("1.10000"), high=Decimal("1.10100"), low=Decimal("1.09900"))

    assert resolve_exit(
        bar=bar, side=Side.BUY, stop=Decimal("1.09700"), target=Decimal("1.10500"),
        contract=_contract(), model=_zero_slip(),
    ) is None


def test_the_same_rules_hold_mirrored_for_a_sell() -> None:
    """Every price comparison in this module has a side. A sign error shows up
    only on the side nobody tested, and a strategy that only goes long would
    never reveal it."""

    bar = _bar(open=Decimal("1.11000"), high=Decimal("1.11100"), low=Decimal("1.10900"))
    exit_ = resolve_exit(
        bar=bar, side=Side.SELL, stop=Decimal("1.10300"), target=None,
        contract=_contract(), model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.kind is ExitKind.STOP
    assert exit_.fill.price == Decimal("1.11000")   # gapped up through a sell's stop
```

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Implement `fills.py`**

For a BUY: the stop is hit when `bar.low <= stop`; the fill is
`min(stop, bar.open)`. The target is hit when `bar.high >= target`; the fill is
exactly `target`. Mirror both for a SELL with `bar.high >= stop`,
`max(stop, bar.open)`, and `bar.low <= target`.

Check the stop **before** the target. That ordering is D-2, and a comment must
say so — an implementer tidying this into "whichever is closer" reintroduces the
defect silently.

Apply `slippage_price_offset` with `opening=False` to every exit fill and
`opening=True` to the entry.

- [ ] **Step 4: Prove the gap rule and the ambiguity rule each bite**

Two mutations, run and reverted separately:
1. Change the stop fill from `min(stop, bar.open)` to `stop`. Confirm
   `test_a_stop_gapped_through_fills_at_the_open_not_the_stop` fails and nothing
   else does.
2. Move the target check above the stop check. Confirm
   `test_a_bar_touching_both_resolves_to_the_stop` fails and nothing else does.

Quote both outputs. Assert each mutation actually applied before trusting it.

- [ ] **Step 5: Run to green, gates, commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/research/backtest/fills.py tests/unit/research/backtest/test_fills.py
git commit -m "feat: the bar fill model, gap rule and stop-first resolution"
```

---

### Task 4: The result

**Files:**
- Create: `src/trading_house/research/backtest/result.py`
- Test: `tests/unit/research/backtest/test_result.py`

**Interfaces:**
- Produces:
  ```python
  class RefusalKind(str, Enum):
      COVERAGE = "coverage"
      DEFECTIVE_BAR = "defective_bar"
      LOOKAHEAD = "lookahead"
      HORIZON = "horizon"

  class SimulatedTrade(CanonicalModel):
      proposal_id: NonEmptyStr
      side: Side
      lots: Decimal
      entry_price: Decimal
      entry_at: datetime
      exit_price: Decimal
      exit_at: datetime
      exit_kind: ExitKind
      gross_pnl: Decimal
      commission: Decimal
      swap: Decimal
      net_pnl: Decimal

  class BacktestResult(CanonicalModel):
      run_id: NonEmptyStr
      strategy_id: NonEmptyStr
      strategy_version: NonEmptyStr
      instrument_id: InstrumentId
      timeframe: Timeframe
      start: datetime
      end: datetime
      firm_equity: Decimal
      cost_model: CostModel
      trades: tuple[SimulatedTrade, ...]
      rejections: tuple[tuple[NonEmptyStr, ...], ...]   # D-8: each decision's reasons
      bars_seen: NonNegativeInt
      net_pnl: Decimal
      def digest(self) -> str
  ```

- [ ] **Step 1: Write the failing tests**

```python
def test_net_pnl_is_gross_less_every_cost_term() -> None:
    """The headline number. If it is computed anywhere but from the trades'
    own fields, the result and its trades can disagree and nothing notices."""

    result = _result(trades=(
        _trade(gross_pnl=Decimal("100"), commission=Decimal("7"), swap=Decimal("-3")),
        _trade(gross_pnl=Decimal("-40"), commission=Decimal("7"), swap=Decimal("0")),
    ))

    assert result.net_pnl == Decimal("43")    # 100-7-3 + (-40-7-0)


def test_a_trade_whose_net_does_not_reconcile_is_refused() -> None:
    """A SimulatedTrade is the audit record of one round trip. One that claims
    a net its own terms do not produce is a bug that would otherwise surface as
    an inexplicable equity curve."""

    with pytest.raises(ValidationError):
        SimulatedTrade(
            proposal_id="p-1", side=Side.BUY, lots=Decimal("0.10"),
            entry_price=Decimal("1.10000"), entry_at=NOW,
            exit_price=Decimal("1.10100"), exit_at=NOW + timedelta(minutes=12),
            exit_kind=ExitKind.TIME,
            gross_pnl=Decimal("100"), commission=Decimal("7"), swap=Decimal("0"),
            net_pnl=Decimal("100"),   # should be 93; the validator must refuse it
        )


def test_the_digest_changes_when_any_cost_input_changes() -> None:
    """Phase 8 hashes this into a trial. Two runs that differ in what they
    assumed costs were must not share a digest, or the trial ledger records a
    Sharpe against the wrong assumptions."""

    base = _result()
    dearer = base.model_copy(
        update={"cost_model": base.cost_model.model_copy(
            update={"commission_per_lot_per_side": Decimal("99")})}
    )

    assert base.digest() != dearer.digest()


def test_the_digest_is_stable_across_equal_results() -> None:
    assert _result().digest() == _result().digest()


def test_rejections_are_carried_not_counted() -> None:
    """D-8. A count says a strategy was vetoed; the reasons say why, and that is
    what tells you whether its edge lived in trades the constitution forbids."""

    result = _result(rejections=(("spread_blowout", "stale_feed"),))

    assert result.rejections == (("spread_blowout", "stale_feed"),)
```

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Implement `result.py`**

`SimulatedTrade` gets a `model_validator(mode="after")` asserting
`net_pnl == gross_pnl - commission + swap` (swap is signed; a charge is
negative). `BacktestResult.net_pnl` gets the same treatment against the sum over
`trades`.

**There is deliberately no equity-curve field**, though §4 of the spec lists
one. Under D-4 equity is constant, so the curve is exactly the running sum of
`net_pnl` over `trades` — storing it would duplicate state that can disagree
with the trades it was derived from, and this plan has already added two
validators whose whole job is catching that class of disagreement. A caller that
wants the curve accumulates it. Say this in the class docstring so the next
reader does not add the field back.

`digest()` returns `hashlib.sha256(self.model_dump_json().encode()).hexdigest()`.
Pydantic serialises fields in declaration order, so this is stable without
sorting — but say that in the docstring, because it is the property Task 6's
cross-process test exists to verify rather than assume.

- [ ] **Step 4: Run to green, gates, commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/research/backtest/result.py tests/unit/research/backtest/test_result.py
git commit -m "feat: the backtest result, its reconciliation and its digest"
```

---

### Task 5: The replay loop

**Files:**
- Create: `src/trading_house/research/backtest/engine.py`
- Test: `tests/unit/research/backtest/test_engine.py`
- Test: `tests/unit/research/backtest/conftest.py`

**Interfaces:**
- Consumes: everything from Tasks 1-4, plus `BarReader`, `FeatureEngine`, `RiskEngine`, `MarginPort`, `Constitution`, `Clock`.
- Produces:
  ```python
  # RefusalKind is defined in Task 4's result.py -- the refusals are part of
  # what a run reports, so they belong with the result rather than the engine.
  class BacktestRefused(Exception):
      def __init__(self, kind: RefusalKind) -> None: ...
      kind: RefusalKind        # no free text: nothing from a broker or a DSN

  @dataclass(frozen=True, slots=True)
  class BacktestRequest:
      strategy: Strategy
      instrument_id: str
      timeframe: Timeframe
      start: datetime
      end: datetime
      firm_equity: Decimal
      cost_model: CostModel
      atr_period: int
      spread_window: int

  class Backtester:
      def __init__(self, *, bars: BarReader, features: FeatureEngine,
                   risk: RiskEngine, margin: MarginPort,
                   contract: InstrumentContract) -> None: ...
      def run(self, request: BacktestRequest) -> BacktestResult: ...
  ```

- [ ] **Step 1: Write the fakes in `conftest.py`**

`FakeBarReader` holds a tuple of `Bar` and implements `bars()` honouring
`as_of`, `start`, `end` and `include_defective`, plus `coverage()` derived from
what it holds. **Make its bar list a public attribute**, so a test staging a
broker changing its mind can set it — a private name silently accepts the
assignment while the fake goes on returning the old value, which is how a test
passes for a reason unrelated to what it checks.

`AlwaysAffordableMargin` implements `MarginPort` returning a large
`free_margin()` and a small `required_margin()`.

`_ramp(n)` returns `n` consecutive M1 `Bar`s starting at
`datetime(2026, 9, 21, 9, 0, tzinfo=UTC)`, each with `open == close == high - 1
point == low + 1 point`, rising by exactly one point per bar from
`Decimal("1.10000")`, `spread=10`, `quality=BarQuality.OK`, and
`availability_time = event_time + duration(Timeframe.M1)`.

`_run(*, bars, strategy, firm_equity=Decimal("100000"), start=None, end=None)`
builds a `Backtester` over `FakeBarReader(bars)`, a real `FeatureEngine`, a real
`RiskEngine` on the test constitution, and `AlwaysAffordableMargin()`, then
calls `run()` with a `BacktestRequest` carrying a `CostModel` whose every field
is stated explicitly.

`_expected_net(result)` recomputes the net from each trade's own
`gross_pnl - commission + swap` and sums — deliberately a second, independent
path to the same number, so the known-answer test fails if the engine and the
result disagree rather than if only one of them is wrong.

`PeekingStrategy` returns a proposal whose `availability_time` is one second
after `snapshot.as_of` and is otherwise identical to `ToyStrategy`'s.

`ToyStrategy` proposes a BUY every `every_n` bars with a fixed
`horizon_seconds=7200` and `max_holding_seconds=7200`, and records every
snapshot it was handed in a public list.

- [ ] **Step 2: Write the failing tests**

```python
def test_a_known_answer_run_produces_exactly_the_hand_computed_trades() -> None:
    """The test this phase exists for. A synthetic series and a toy whose every
    entry and exit can be worked out on paper -- which is what validates the
    SIMULATOR rather than a strategy, and is why Phase 6 is split from Phase 7."""

    # 60 M1 bars rising by exactly 1 point each, spread fixed at 10 points.
    # ToyStrategy buys on bar 20 and bar 40; each is held 12 bars then time-stopped.
    result = _run(bars=_ramp(60), strategy=ToyStrategy(every_n=20))

    assert [trade.exit_kind for trade in result.trades] == [ExitKind.TIME, ExitKind.TIME]
    assert result.trades[0].entry_price == Decimal("1.10026")   # bar 21 open + half spread
    assert result.trades[0].exit_price == Decimal("1.10032")    # bar 33 open - half spread
    assert result.net_pnl == _expected_net(result)


def test_exits_are_resolved_before_new_entries_are_considered() -> None:
    """Reversed, a strategy could be opening while it is in fact already
    stopped out on the same bar -- and the run would show two open positions
    where the live system would have had one."""

    result = _run(bars=_ramp(40), strategy=ToyStrategy(every_n=1))

    assert all(
        earlier.exit_at <= later.entry_at
        for earlier, later in zip(result.trades, result.trades[1:], strict=False)
    )


def test_only_one_position_is_open_at_a_time() -> None:
    """D-7. max_concurrent_positions is enforced nowhere at decision time, so a
    simulator opening many would report risk the live engine permits while
    nothing in the live path limits it."""

    result = _run(bars=_ramp(60), strategy=ToyStrategy(every_n=1))

    for earlier, later in zip(result.trades, result.trades[1:], strict=False):
        assert earlier.exit_at <= later.entry_at


def test_the_strategy_never_sees_a_bar_that_had_not_closed() -> None:
    """Point-in-time discipline, checked on the snapshots the strategy actually
    received rather than on the store's filtering. Bar.availability_time >
    event_time is a schema invariant; this asserts the simulator honours it."""

    strategy = ToyStrategy(every_n=5)
    _run(bars=_ramp(30), strategy=strategy)

    for snapshot in strategy.seen:
        assert snapshot.as_of == snapshot.bar.availability_time
        assert snapshot.bar.event_time < snapshot.as_of


def test_a_rejected_decision_is_recorded_with_its_reasons() -> None:
    """D-8. A strategy whose edge lives in trades the constitution refuses has
    no edge in this system, and a count alone would not say which trades."""

    result = _run(bars=_ramp(30), strategy=ToyStrategy(every_n=5),
                  firm_equity=Decimal("1"))   # too small to size anything

    assert result.trades == ()
    assert result.rejections != ()


def test_a_horizon_under_ten_bars_refuses_the_run() -> None:
    with pytest.raises(BacktestRefused) as caught:
        _run(bars=_ramp(30), strategy=ToyStrategy(every_n=5, horizon_seconds=60))

    assert caught.value.kind is RefusalKind.HORIZON


def test_a_defective_bar_in_the_range_refuses_the_run() -> None:
    """The ingester already flagged this bar as wrong. Trading on it anyway is
    the simulator overruling a judgement made with more information."""

    bars = _ramp(30)
    poisoned = bars[:15] + (bars[15].model_copy(
        update={"quality": BarQuality.OHLC_INCOHERENT}),) + bars[16:]

    with pytest.raises(BacktestRefused) as caught:
        _run(bars=poisoned, strategy=ToyStrategy(every_n=5))

    assert caught.value.kind is RefusalKind.DEFECTIVE_BAR


def test_a_range_outside_the_stores_coverage_refuses_the_run() -> None:
    """Simulating across data we do not have is the worst kind of silent lie:
    the equity curve simply has fewer bars than the period claims."""

    with pytest.raises(BacktestRefused) as caught:
        _run(bars=_ramp(30), strategy=ToyStrategy(every_n=5),
             start=_ramp(30)[0].event_time - timedelta(days=30))

    assert caught.value.kind is RefusalKind.COVERAGE


def test_a_strategy_that_peeks_is_refused() -> None:
    """The classic leak. TradeProposal is Stamped, so a proposal claiming
    availability later than the snapshot that produced it is detectable for
    free -- and a strategy that peeked must be refused, not scored."""

    with pytest.raises(BacktestRefused) as caught:
        _run(bars=_ramp(30), strategy=PeekingStrategy())

    assert caught.value.kind is RefusalKind.LOOKAHEAD
```

- [ ] **Step 3: Run them and watch them fail.**

- [ ] **Step 4: Implement `engine.py`**

The loop, in this order per bar — and the order is the design:

1. Resolve any open position against this bar via `resolve_exit`.
2. `as_of = bar.availability_time`.
3. Build the `FeatureSnapshot` from `features.atr(...)` and
   `features.median_spread_points(...)` at that `as_of`, with
   `tick_spread_points=Decimal(bar.spread)` and `tick_time=as_of`.
4. `strategy.evaluate(snapshot)`.
5. If a proposal: assert `proposal.availability_time <= snapshot.as_of`, else
   raise `BacktestRefused(RefusalKind.LOOKAHEAD)`.
6. `risk.evaluate_for_execution(...)` with `firm_equity` from the request.
7. On `RejectedRiskDecision`, append its `reasons` to `rejections` and continue.
8. On an executable decision, **read `approved_quantity` and `stop_loss_price`
   off it** — do not call `compute_volume` or `stop_price`; the engine already
   did, and calling them again would size twice.
9. Queue the entry to fill on the **next** bar via `entry_fill`.

Refusals are checked up front where they can be: horizon before the loop starts,
coverage before reading bars, and defective bars as they are read.

**Coverage means the boundary, not interior holes.** `Coverage` exposes
`earliest_event_time` / `latest_event_time` and bar counts, not gaps. Refuse when
the requested range is not inside that window. Do **not** refuse on interior gaps
— FX closes every weekend, so that rule would refuse every run spanning a
Saturday. The spec's §8 row says "coverage gap"; this is what it means in terms
the store can actually answer, and the docstring must say so.

- [ ] **Step 5: Prove the ordering and the sizing each bite**

1. Move the exit resolution after `strategy.evaluate`. Confirm
   `test_exits_are_resolved_before_new_entries_are_considered` fails.
2. Replace the read of `decision.approved_quantity` with a local
   `compute_volume(...)` call. Confirm the known-answer test fails on the lot
   size, which is what proves D-3's "the engine sizes, the simulator does not".

Revert both, quote both outputs, and check `git status --porcelain` is clean
before committing.

- [ ] **Step 6: Run to green, gates, commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/research/backtest -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/research/backtest/engine.py tests/unit/research/backtest
git commit -m "feat: the backtest replay loop and its four refusals"
```

---

### Task 6: The command, determinism across processes, and the boundary

**Files:**
- Modify: `src/trading_house/cli.py`
- Modify: `tests/acceptance/test_architecture.py`
- Create: `tests/acceptance/test_phase6.py`
- Create: `tests/integration/research/test_backtest_determinism.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `Backtester`, `BacktestRequest`, `BacktestResult`.
- Produces: `trading-house backtest run`, emitting the result as deterministic key-sorted JSON on stdout.

- [ ] **Step 1: Add the architecture arrow**

In `tests/acceptance/test_architecture.py`, add `BACKTEST_ROOT` and
`BACKTEST_FORBIDDEN = frozenset({"trading_house.brokers", "trading_house.execution"})`
following the four existing pairs exactly, with a
`test_no_backtest_module_imports_a_broker_or_the_execution_plane` and a
parametrized guard-the-guard case proving the detector still fires on
`from trading_house.execution import loop`.

- [ ] **Step 2: Add `backtest run` to the CLI**

Follow `guard_app`'s shape. It takes the strategy's registered id, the
instrument, the timeframe, the range, the equity, and the cost-model fields —
**every cost field required, none defaulted** (D-5). Emit
`result.model_dump_json()` plus the digest.

For Phase 6 the only registrable strategy is the toy, exposed through a small
`--toy-every-n` option. Phase 7 replaces that with a real registry; say so in a
comment so nobody mistakes the toy for a strategy worth running.

- [ ] **Step 3: Write the cross-process determinism test**

```python
@pytest.mark.integration
def test_two_runs_under_different_hash_seeds_agree_byte_for_byte() -> None:
    """Phase 8 hashes this result into a trial, and a Sharpe nobody can
    reproduce is not evidence.

    Two SUBPROCESSES under different PYTHONHASHSEED, not two calls in one
    process. Calling a function twice in one interpreter proves almost nothing
    -- this project shipped exactly that hollow fixture in Phase 2 -- and set or
    dict ordering reaching the output is the realistic leak that only a
    different seed exposes."""

    first = _run_cli(hash_seed="0")
    second = _run_cli(hash_seed="12345")

    assert first == second
    assert json.loads(first)["digest"]
```

`_run_cli` invokes `sys.executable -m trading_house.cli backtest run ...` via
`subprocess.run` with `env={**os.environ, "PYTHONHASHSEED": hash_seed}` and
returns `stdout`.

- [ ] **Step 4: Write the phase acceptance test**

`tests/acceptance/test_phase6.py` pins the two properties a future change
must not quietly lose: that `CostModel` has no defaulted cost field (construct
it with one omitted and assert `ValidationError`), and that `research/backtest/`
contains no call to `compute_volume` or `stop_price` — an AST check, like
Phase 5's `amend_protection` detector, with its own guard-the-guard case.

That second one is the structural form of D-3: the simulator must not size.

- [ ] **Step 5: Document**

Add a "Phase 6 — the backtester" section to `README.md` stating what the
simulator promises and, as plainly, what it does not: bar resolution only, no
strategy with an edge, market impact and inference cost not modelled, and the
four refusals. State that `max_concurrent_positions` is enforced nowhere at
decision time and that D-7 sidesteps rather than fixes it.

- [ ] **Step 6: Full gate, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run ruff format --check .
UV_SYSTEM_CERTS=1 uv run ruff check .
UV_SYSTEM_CERTS=1 uv run mypy
UV_SYSTEM_CERTS=1 uv run pytest -q
git add src tests README.md
git commit -m "feat: the backtest command, its determinism proof and its boundary"
```
