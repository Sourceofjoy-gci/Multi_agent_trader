# Phase 3 — Risk and Position Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert a `TradeProposal` plus market facts into a `RiskDecision` — a lot size and a stop price, or a refusal with reasons — using arithmetic whose realised risk can never exceed the book's budget.

**Architecture:** Two modules under `risk/`. `sizing.py` is pure arithmetic over `Decimal`; `engine.py` holds the constitution and a clock, runs the stateless gates, and exposes a second entry point that adds the margin-headroom check behind a narrow Protocol. The stop distance quantises up to the tick grid *before* the volume is computed from it, so both roundings push the same way.

**Tech Stack:** Python 3.12, Pydantic v2 (strict/frozen/extra=forbid), `Decimal` throughout, pytest, Hypothesis, mypy strict, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-06-phase-3-risk-sizing-design.md`

## Global Constraints

- **Every `uv` command must be prefixed `UV_SYSTEM_CERTS=1`** or it fails TLS verification in this environment.
- The mypy gate is `UV_SYSTEM_CERTS=1 uv run mypy` with **no path arguments** — the project scopes it via `packages = ["trading_house"]`. Passing paths hits an unrelated pre-existing basename collision.
- **`risk/` imports `core/` and `constitution/` and nothing else from this project** — not `brokers/`, not `marketdata/`, not `features/`. An acceptance test pins it.
- Line length 100. mypy strict. **No new `# type: ignore` in `src/`.**
- **Do not add a `# noqa` for a rule this project does not enable.** The ruff select list is exactly `["E", "F", "I", "B", "UP", "SIM", "RUF", "S", "PT"]`. A directive for anything outside it suppresses nothing and `RUF100` fails the build. This defect class has cost this project five rounds across earlier phases.
- **All money and price values are `Decimal`.** A float must never reach a stop distance, a lot size or a risk figure. `float()` is permitted only to build a `timedelta` threshold.
- Enums subclass `str, Enum` with `# noqa: UP042`, matching `core.schemas.Side`. This project has no `StrEnum` anywhere; do not introduce one.
- The baseline suite is **868 passed / 9 skipped at 98.05% coverage**. Run `UV_SYSTEM_CERTS=1 uv run pytest -q` before committing; anything less is a regression.

## What Already Exists

Do not redefine any of this. It is all verified present at `main` (`2b8d5eb`).

```python
# trading_house.core.instruments
class InstrumentContract(CanonicalModel):
    instrument_id: InstrumentId
    asset_class: AssetClass
    base_currency: NonEmptyStr
    quote_currency: NonEmptyStr
    price_increment: PositiveDecimal        # MT5 trade_tick_size
    quantity_increment: PositiveDecimal     # volume_step
    quantity_min: PositiveDecimal
    quantity_max: PositiveDecimal
    value_per_price_increment: PositiveDecimal   # MT5 trade_tick_value_loss
    min_stop_distance: PositiveDecimal      # price units, floored to >= price_increment
    freeze_distance: PositiveDecimal        # price units, floored to >= price_increment
    session_calendar_id: NonEmptyStr
    financing: FinancingModel
    can_open_long: bool
    can_open_short: bool
    supported_fills: frozenset[FillPolicy]

# trading_house.core.schemas
class Side(str, Enum):        # BUY = "BUY", SELL = "SELL"

class TradeProposal(Stamped):
    proposal_id: NonEmptyStr
    strategy_id: NonEmptyStr
    strategy_version: NonEmptyStr
    book: BookId
    instrument_id: InstrumentId
    side: Side
    horizon_seconds: PositiveInt
    entry_condition: NonEmptyStr
    entry_price_ref: Price
    invalidation_price: Price       # validated onto the loss side of entry_price_ref
    max_holding_seconds: PositiveInt
    expected_return_bps: FiniteFloat
    expected_return_stdev_bps: NonNegativeFiniteFloat
    expected_cost_bps: NonNegativeFiniteFloat
    win_probability: Probability    # 0 and 1 both rejected
    calibration_id: NonEmptyStr
    required_liquidity: PositiveQuantity
    regime_ref: NonEmptyStr
    features_snapshot_id: NonEmptyStr
    rationale: NonEmptyStr | None = None

# Stamped (TradeProposal's base) requires, in addition:
#   event_time, availability_time, processing_time  (aware UTC, non-decreasing in that order)
#   source: NonEmptyStr

class BaseRiskDecision(CanonicalModel):
    proposal_id: NonEmptyStr
    reasons: tuple[NonEmptyStr, ...]
    checks_passed: tuple[NonEmptyStr, ...]
    constitution_version: PositiveInt

class ExecutableRiskDecision(BaseRiskDecision):
    approved_quantity: PositiveQuantity
    stop_loss_price: Price               # NOT optional — a protective stop is structural
    take_profit_price: Price | None
    risk_money: Decimal                  # gt=0
    risk_pct_of_book: Decimal            # gt=0

class ApprovedRiskDecision(ExecutableRiskDecision):  verdict: Literal["APPROVED"]
class ResizedRiskDecision(ExecutableRiskDecision):   verdict: Literal["RESIZED"]
class RejectedRiskDecision(BaseRiskDecision):
    verdict: Literal["REJECTED"]
    approved_quantity: Quantity          # amount must be 0
    stop_loss_price: None = None
    take_profit_price: None = None
    risk_money: Decimal                  # pinned to exactly 0 (ge=0, le=0)
    risk_pct_of_book: Decimal            # pinned to exactly 0
    # at least one reason required

RiskDecision = Annotated[ApprovedRiskDecision | ResizedRiskDecision | RejectedRiskDecision,
                         Field(discriminator="verdict")]

# trading_house.core.values
class Quantity(CanonicalModel):  amount: Decimal (ge=0);  unit: QuantityUnit
class PositiveQuantity(Quantity): amount: Decimal (gt=0)
QuantityUnit = Literal["lots", "shares", "base_units", "contracts"]

# trading_house.core.clock
class Clock(Protocol):
    def now(self) -> datetime: ...
def ensure_utc(value: datetime) -> datetime          # raises TimestampError if naive

# trading_house.constitution.models
class BookLimits(ConstitutionModel):
    capital_fraction, horizon, asset_classes, risk_per_trade_pct,
    max_concurrent_positions, daily_loss_stop_pct, max_drawdown_halt_pct,
    max_gross_leverage, limits: ScalpLimits | SwingLimits
class ScalpLimits(ConstitutionModel):  max_spread_multiple_at_entry: PositiveDecimal, ...
class SafeModeTriggers(ConstitutionModel):
    max_tick_age_seconds: PositiveDecimal
    max_spread_multiple_of_median: PositiveDecimal
    ...
class Constitution(ConstitutionModel):
    version: PositiveInt
    books: Mapping[BookId, BookLimits]
    safe_mode_triggers: Mapping[Horizon, SafeModeTriggers]
    ...
```

**The four books in `config/risk_constitution.yaml`**, with their horizons and capital fractions — these exact ids appear in tests:

| book | horizon | capital_fraction | risk_per_trade_pct |
|---|---|---|---|
| `fx_scalp` | scalp | 0.30 | 0.25 |
| `fx_swing` | swing | 0.45 | 0.75 |
| `equity_swing` | swing | 0.15 | 0.50 |
| `sleeve` | swing | 0.10 | 1.5 |

## File Structure

| File | Responsibility |
|---|---|
| `core/instruments.py` | gains `point_size` on `InstrumentContract` |
| `brokers/mt5/contracts.py` | populates `point_size` from the `point` it already reads |
| `constitution/models.py` | `BookLimits` gains `k_sigma`, `k_spread` |
| `config/risk_constitution.yaml` | the two new values per book, then re-signed |
| `risk/sizing.py` | `quantise_up`, `quantise_down`, `compute_stop_distance`, `compute_volume`, `stop_price` — pure |
| `risk/engine.py` | `RejectionReason`, `MarginPort`, `RiskEngine` |
| `tests/unit/risk/test_sizing.py` | the hand-worked table, the four terms, the boundaries |
| `tests/unit/risk/test_engine.py` | every gate, both margin directions |
| `tests/property/test_risk.py` | the I-19 property |
| `tests/acceptance/test_phase3.py` | phase-level guarantees |
| `tests/acceptance/test_architecture.py` | `risk/`'s import arrow |

---

### Task 1: Widen the inputs — point size and the stop multipliers

Nothing in Phase 3 can be computed until the contract can price a spread point and the constitution carries the two multipliers. This task changes three source files, one config file, and every fixture that constructs the models involved.

**Files:**
- Modify: `src/trading_house/core/instruments.py`
- Modify: `src/trading_house/brokers/mt5/contracts.py`
- Modify: `src/trading_house/constitution/models.py`
- Modify: `config/risk_constitution.yaml`
- Modify (fixtures): `tests/unit/core/test_instruments.py`, `tests/unit/constitution/test_models.py`, `tests/unit/test_settings.py`
- Test: the three files above plus `tests/unit/brokers/mt5/test_contracts.py`

**Interfaces:**
- Consumes: nothing from other tasks — this is the first task.
- Produces: `InstrumentContract.point_size: PositiveDecimal`; `BookLimits.k_sigma: PositiveDecimal`; `BookLimits.k_spread: PositiveDecimal`.

**Warning — a required field breaks every construction site.** `InstrumentContract` is built at 9 places across `core/instruments.py`, `brokers/mt5/contracts.py` and `tests/unit/core/test_instruments.py`. `BookLimits` is never constructed directly; it is only parsed from YAML, so its fixtures are the inline dicts in `tests/unit/constitution/test_models.py` and `tests/unit/test_settings.py`. Grep before you assume you have found them all:

```bash
grep -rn "InstrumentContract(" src/ tests/ scripts/ --include=*.py
grep -rn "capital_fraction" tests/ --include=*.py
```

- [ ] **Step 1: Write the failing test for `point_size` on the contract mapping**

In `tests/unit/brokers/mt5/test_contracts.py`, following the fixture style already in that file:

```python
def test_point_size_is_carried_through_from_the_symbol_info() -> None:
    """A stored Bar.spread is an integer in MT5 points. Without the point size
    on the neutral contract, nothing downstream -- the backtester included --
    can convert that spread into a price. contracts.py already reads `point`
    to build min_stop_distance and then throws it away."""

    info = _symbol_info(point=0.00001, trade_tick_size=0.00001)

    contract = to_instrument_contract(info, instrument_id="fx.eurusd")

    assert contract.point_size == Decimal("0.00001")


def test_point_size_is_independent_of_price_increment() -> None:
    """point and trade_tick_size are equal on most FX symbols and are not the
    same field. A mapping that aliased one to the other would pass the test
    above and be wrong on any symbol where they differ."""

    info = _symbol_info(point=0.001, trade_tick_size=0.005)

    contract = to_instrument_contract(info, instrument_id="metal.xauusd")

    assert contract.point_size == Decimal("0.001")
    assert contract.price_increment == Decimal("0.005")
```

- [ ] **Step 2: Run it and watch it fail**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/brokers/mt5/test_contracts.py -q
```

Expected: FAIL — `InstrumentContract` has no attribute `point_size`.

- [ ] **Step 3: Add the field and populate it**

In `core/instruments.py`, add to `InstrumentContract` immediately after `price_increment`:

```python
    # MT5's `point`. Equal to price_increment on most FX symbols and NOT the
    # same field: a stored Bar.spread is an integer count of these, so without
    # it a spread cannot be converted to a price distance.
    point_size: PositiveDecimal
```

In `brokers/mt5/contracts.py`, inside `to_instrument_contract`, add to the constructor call after `price_increment=price_increment,`:

```python
        point_size=point,
```

`point` is already computed on the line `point = decimal_of(info.point)`; do not compute it again.

- [ ] **Step 4: Run the contract tests and the full unit suite**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit -q
```

Expected: the two new tests PASS; several tests in `tests/unit/core/test_instruments.py` FAIL with a Pydantic missing-field error. That is the required-field breakage — fix each construction site by adding a `point_size` equal to that fixture's `price_increment` unless the test is specifically about them differing.

- [ ] **Step 5: Write the failing test for the constitution multipliers**

In `tests/unit/constitution/test_models.py`, following the inline-dict style already there:

```python
def test_book_limits_require_both_stop_multipliers() -> None:
    """k_sigma and k_spread are risk parameters: halving k_sigma halves every
    stop distance and roughly doubles every position size. A book that omits
    them must not load, because a default would be an unsigned risk decision."""

    payload = _book_payload()
    del payload["k_sigma"]

    with pytest.raises(ValidationError):
        BookLimits.model_validate(payload)


def test_stop_multipliers_must_be_positive() -> None:
    """A zero k_sigma removes the volatility term from the stop distance
    entirely and leaves the max() to the broker floor."""

    with pytest.raises(ValidationError):
        BookLimits.model_validate(_book_payload(k_sigma=Decimal("0")))


def test_stop_multipliers_accept_yaml_integers() -> None:
    """YAML `k_sigma: 2` parses as int, and strict mode rejects an int for a
    Decimal field unless the before-validator converts it, exactly as every
    other Decimal on this model does."""

    limits = BookLimits.model_validate(_book_payload(k_sigma=2, k_spread=3))

    assert limits.k_sigma == Decimal("2")
    assert limits.k_spread == Decimal("3")
```

Add a `_book_payload(**overrides)` helper to that file if one does not already exist, returning a complete valid `BookLimits` dict (copy the `fx_scalp` shape from `config/risk_constitution.yaml`) with `overrides` applied.

- [ ] **Step 6: Run it and watch it fail**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/constitution -q
```

Expected: FAIL — `extra="forbid"` rejects `k_sigma`.

- [ ] **Step 7: Add the fields to `BookLimits`**

In `constitution/models.py`, add to `BookLimits` after `max_gross_leverage`:

```python
    # Stop-distance multipliers, consumed by risk/sizing.py. They sit behind the
    # signature because halving k_sigma halves every stop and roughly doubles
    # every position size -- the same reason risk_per_trade_pct is here.
    k_sigma: PositiveDecimal
    k_spread: PositiveDecimal
```

and add `"k_sigma"` and `"k_spread"` to the existing `convert_integer_decimals` `@field_validator` tuple on that class, so that `k_sigma: 2` in YAML parses under strict mode.

- [ ] **Step 8: Add the values to the signed constitution**

In `config/risk_constitution.yaml`, add to each of the four books, immediately after that book's `max_gross_leverage`:

```yaml
    # Stop-distance multipliers (master spec 8.2). Starting points: scalp uses
    # 1.2x ATR (the table's 1.0-1.5 band, above the ~65% noise stop-out floor),
    # swing uses 2.5x (the low end of its 2.5-3.0 band). k_spread 2.0 requires
    # every stop to clear twice the median spread, so a trade cannot be stopped
    # out by its own round-trip cost.
    k_sigma: 1.2
    k_spread: 2.0
```

using `k_sigma: 1.2` for `fx_scalp` and `k_sigma: 2.5` for `fx_swing`, `equity_swing` and `sleeve`. `k_spread: 2.0` for all four. Put the comment block on `fx_scalp` only.

- [ ] **Step 9: Re-sign the constitution**

Changing the YAML invalidates the detached signature, and several tests verify the shipped pair. **This step is run by the controller, not by an implementer subagent** — see the plan's handoff note.

```bash
UV_SYSTEM_CERTS=1 uv run trading-house constitution sign --constitution config/risk_constitution.yaml --private-key .local/keys/risk_constitution.private.pem --signature-output config/risk_constitution.yaml.sig --force
UV_SYSTEM_CERTS=1 uv run trading-house constitution verify
```

Expected: `verify` reports `constitution_version: 1` and a fresh `constitution_sha256`.

- [ ] **Step 10: Run the whole suite**

```bash
UV_SYSTEM_CERTS=1 uv run pytest -q
```

Expected: 868 + the new tests, still 9 skipped, coverage at or above 98%. If `tests/property/test_constitution_signatures.py` or `tests/acceptance/test_phase0.py` fails, the re-sign in Step 9 did not happen or wrote to the wrong path.

- [ ] **Step 11: Commit**

```bash
git add src/trading_house/core/instruments.py src/trading_house/brokers/mt5/contracts.py src/trading_house/constitution/models.py config/risk_constitution.yaml config/risk_constitution.yaml.sig tests/
git commit -m "feat: carry point size on the contract and stop multipliers in the constitution"
```

---

### Task 2: The pure arithmetic

**Files:**
- Create: `src/trading_house/risk/__init__.py` (empty)
- Create: `src/trading_house/risk/sizing.py`
- Create: `tests/unit/risk/conftest.py` — the shared `_contract` builder
- Test: `tests/unit/risk/test_sizing.py`

**Where shared test helpers go.** This repo already has a convention: plain
functions are defined in a `conftest.py` and imported directly, e.g.
`from tests.unit.brokers.mt5.conftest import FakeTerminal` in
`tests/unit/brokers/mt5/test_adapter.py`. Follow it — put `_contract` in
`tests/unit/risk/conftest.py` and import it wherever it is needed. Do not
define a second copy anywhere; two fixtures that drift apart is precisely the
defect that made Phase 2's determinism test hollow.

**Interfaces:**
- Consumes: `InstrumentContract.point_size` and `BookLimits.k_sigma` / `k_spread` from Task 1; `Side` from `core.schemas`.
- Produces, for Task 3:
  ```python
  def quantise_up(value: Decimal, increment: Decimal) -> Decimal
  def quantise_down(value: Decimal, increment: Decimal) -> Decimal
  def compute_stop_distance(*, atr: Decimal, median_spread_points: Decimal,
                            entry_price_ref: Decimal, invalidation_price: Decimal,
                            contract: InstrumentContract,
                            k_sigma: Decimal, k_spread: Decimal) -> Decimal
  def compute_volume(*, stop_distance: Decimal, book_equity: Decimal,
                     risk_per_trade_pct: Decimal, contract: InstrumentContract) -> Decimal
  def stop_price(*, side: Side, entry_price_ref: Decimal, stop_distance: Decimal,
                 contract: InstrumentContract) -> Decimal
  ```

- [ ] **Step 1: Write the quantiser tests**

```python
# tests/unit/risk/conftest.py
"""Shared builders for the risk tests, imported rather than duplicated."""

from decimal import Decimal

from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.values import AssetClass


def _contract(**overrides: object) -> InstrumentContract:
    """A 5-digit FX contract. 1 lot of EURUSD moves $1 per 0.00001 of price."""

    fields: dict[str, object] = {
        "instrument_id": "fx.eurusd",
        "asset_class": AssetClass.FX,
        "base_currency": "EUR",
        "quote_currency": "USD",
        "price_increment": Decimal("0.00001"),
        "point_size": Decimal("0.00001"),
        "quantity_increment": Decimal("0.01"),
        "quantity_min": Decimal("0.01"),
        "quantity_max": Decimal("100"),
        "value_per_price_increment": Decimal("1"),
        "min_stop_distance": Decimal("0.00001"),
        "freeze_distance": Decimal("0.00001"),
        "session_calendar_id": "fx.default",
        "financing": FinancingModel.SWAP,
        "can_open_long": True,
        "can_open_short": True,
        "supported_fills": frozenset({FillPolicy.IOC}),
    }
    fields.update(overrides)
    return InstrumentContract.model_validate(fields)
```

Then the test module itself:

```python
# tests/unit/risk/test_sizing.py
"""Pure sizing arithmetic. Every expected value here is worked longhand in a
comment so a reader can verify it without running the code."""

from decimal import Decimal

import pytest

from tests.unit.risk.conftest import _contract
from trading_house.core.schemas import Side
from trading_house.risk.sizing import (
    compute_stop_distance,
    compute_volume,
    quantise_down,
    quantise_up,
    stop_price,
)


@pytest.mark.parametrize(
    ("value", "increment", "expected"),
    [
        ("0.00013", "0.00001", "0.00013"),   # already on the grid, unchanged
        ("0.000131", "0.00001", "0.00014"),  # rounds up to the next tick
        ("0.000139", "0.00001", "0.00014"),
        ("0", "0.00001", "0"),
    ],
)
def test_quantise_up_never_lands_below_its_input(
    value: str, increment: str, expected: str
) -> None:
    result = quantise_up(Decimal(value), Decimal(increment))

    assert result == Decimal(expected)
    assert result >= Decimal(value)


@pytest.mark.parametrize(
    ("value", "increment", "expected"),
    [
        ("0.37", "0.01", "0.37"),
        ("0.379", "0.01", "0.37"),
        ("0.371", "0.01", "0.37"),
        ("0.009", "0.01", "0"),
    ],
)
def test_quantise_down_never_lands_above_its_input(
    value: str, increment: str, expected: str
) -> None:
    result = quantise_down(Decimal(value), Decimal(increment))

    assert result == Decimal(expected)
    assert result <= Decimal(value)


def test_quantisers_refuse_a_non_positive_increment() -> None:
    with pytest.raises(ValueError, match="increment"):
        quantise_up(Decimal("1"), Decimal("0"))
    with pytest.raises(ValueError, match="increment"):
        quantise_down(Decimal("1"), Decimal("-0.01"))


def test_quantisers_refuse_a_negative_value() -> None:
    """Decimal's divmod truncates toward zero rather than flooring, so a
    negative input would round the wrong way silently."""

    with pytest.raises(ValueError, match="negative"):
        quantise_down(Decimal("-1"), Decimal("0.01"))
```

- [ ] **Step 2: Run and watch it fail**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk/test_sizing.py -q
```

Expected: FAIL — `trading_house.risk` does not exist.

- [ ] **Step 3: Write the quantisers**

```python
# src/trading_house/risk/sizing.py
"""Stop-distance and volume arithmetic. Pure: no I/O, no clock, no constitution.

The order of rounding is the point of this module. The stop distance rounds UP
to the tick grid and the volume rounds DOWN to the lot grid, so both roundings
push the same way and realised risk is bounded above by the budget instead of
straddling it. Reversing either one lets a position exceed the signed risk
budget on roughly half of all trades.
"""

from __future__ import annotations

from decimal import Decimal

from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import Side


def _checked(value: Decimal, increment: Decimal) -> None:
    if increment <= 0:
        raise ValueError("increment must be positive")
    if value < 0:
        raise ValueError("value must not be negative")


def quantise_up(value: Decimal, increment: Decimal) -> Decimal:
    """The smallest multiple of ``increment`` that is not below ``value``."""

    _checked(value, increment)
    whole, remainder = divmod(value, increment)
    return whole * increment if remainder == 0 else (whole + 1) * increment


def quantise_down(value: Decimal, increment: Decimal) -> Decimal:
    """The largest multiple of ``increment`` that is not above ``value``."""

    _checked(value, increment)
    whole, _ = divmod(value, increment)
    return whole * increment
```

`divmod` on `Decimal` is exact — it computes the true integer quotient rather than dividing at the context precision, so `divmod(Decimal("0.00013"), Decimal("0.00001"))` gives `(13, 0)` and not `(12, 0.00000999…)`. Do not replace it with `(value / increment).to_integral_value(...)`.

- [ ] **Step 4: Run and watch it pass**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk/test_sizing.py -q
```

- [ ] **Step 5: Write the stop-distance tests — each term must win in turn**

This is the test that guards against a dead term. A term nobody proves load-bearing is a term that could be deleted with the suite still green.

```python
def test_the_volatility_term_wins_when_it_is_largest() -> None:
    """k_sigma 2.0 x ATR 0.00050 = 0.00100, above every other term."""

    distance = compute_stop_distance(
        atr=Decimal("0.00050"),
        median_spread_points=Decimal("10"),          # cost: 2.0 x 10 x 0.00001 = 0.00020
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09970"),       # structural: 0.00030
        contract=_contract(),                        # floor: 0.00001
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00100")


def test_the_cost_term_wins_when_the_spread_is_wide() -> None:
    """k_spread 2.0 x 60 points x 0.00001 = 0.00120, above the 0.00040
    volatility term and the 0.00030 structural term."""

    distance = compute_stop_distance(
        atr=Decimal("0.00020"),
        median_spread_points=Decimal("60"),
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09970"),
        contract=_contract(),
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00120")


def test_the_structural_term_wins_when_invalidation_is_far() -> None:
    """|1.10000 - 1.09250| = 0.00750, above the 0.00040 volatility term."""

    distance = compute_stop_distance(
        atr=Decimal("0.00020"),
        median_spread_points=Decimal("10"),
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09250"),
        contract=_contract(),
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00750")


def test_the_broker_floor_wins_when_every_other_term_is_tiny() -> None:
    """A broker demanding 50 points of stop distance overrides a quiet market."""

    distance = compute_stop_distance(
        atr=Decimal("0.00001"),
        median_spread_points=Decimal("1"),
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09999"),
        contract=_contract(min_stop_distance=Decimal("0.00050")),
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00050")


def test_the_floor_takes_freeze_distance_when_it_is_the_wider_of_the_two() -> None:
    """A stop inside the freeze band is legal to place and illegal to modify,
    which would hand the position guard an untouchable stop on a live
    position. The master spec's 8.2 code uses only stops_level; its comment
    says stops/freeze, and the comment is the correct reading."""

    distance = compute_stop_distance(
        atr=Decimal("0.00001"),
        median_spread_points=Decimal("1"),
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09999"),
        contract=_contract(
            min_stop_distance=Decimal("0.00020"),
            freeze_distance=Decimal("0.00080"),
        ),
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00080")


def test_the_distance_is_quantised_up_to_the_tick_grid() -> None:
    """k_sigma 1.5 x ATR 0.000333 = 0.0004995, which is not on a 0.00001 grid.
    Rounding down would place the stop nearer than the risk model demanded."""

    distance = compute_stop_distance(
        atr=Decimal("0.000333"),
        median_spread_points=Decimal("1"),
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09999"),
        contract=_contract(),
        k_sigma=Decimal("1.5"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00050")


def test_the_cost_term_uses_point_size_not_price_increment() -> None:
    """On a symbol where they differ, pricing the spread with price_increment
    inflates the cost term fivefold. 2.0 x 30 points x 0.001 = 0.06."""

    distance = compute_stop_distance(
        atr=Decimal("0.001"),
        median_spread_points=Decimal("30"),
        entry_price_ref=Decimal("2000.000"),
        invalidation_price=Decimal("1999.999"),
        contract=_contract(
            price_increment=Decimal("0.005"),
            point_size=Decimal("0.001"),
            min_stop_distance=Decimal("0.005"),
            freeze_distance=Decimal("0.005"),
        ),
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.060")
```

- [ ] **Step 6: Run and watch them fail**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk/test_sizing.py -q
```

Expected: FAIL — `compute_stop_distance` is not defined.

- [ ] **Step 7: Write `compute_stop_distance`**

Append to `risk/sizing.py`:

```python
def compute_stop_distance(
    *,
    atr: Decimal,
    median_spread_points: Decimal,
    entry_price_ref: Decimal,
    invalidation_price: Decimal,
    contract: InstrumentContract,
    k_sigma: Decimal,
    k_spread: Decimal,
) -> Decimal:
    """The master spec's 8.2 four-term maximum, rounded up to the tick grid.

    The cost term takes the MEDIAN spread, not the current one: a single
    spike must not widen the stop and therefore shrink the position. The
    current spread is a gate in the engine, not an input here.
    """

    volatility = k_sigma * atr
    cost = k_spread * median_spread_points * contract.point_size
    structural = abs(entry_price_ref - invalidation_price)
    broker_floor = max(contract.min_stop_distance, contract.freeze_distance)
    widest = max(volatility, cost, structural, broker_floor)
    return quantise_up(widest, contract.price_increment)
```

The result is always positive: `broker_floor` is at least one `price_increment`, because `contracts.py` floors both distances there.

- [ ] **Step 8: Run and watch them pass**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk/test_sizing.py -q
```

- [ ] **Step 9: Write the volume tests — the hand-worked table**

```python
def test_volume_against_a_hand_worked_example() -> None:
    """EURUSD, 1 lot moves $1.00 per 0.00001 of price.

        budget        = 10_000 x 0.25 / 100          = 25.00
        ticks         = 0.00100 / 0.00001            = 100
        money_per_lot = 100 x 1.00                   = 100.00
        raw volume    = 25.00 / 100.00               = 0.25
        on the grid   = floor(0.25 / 0.01) x 0.01    = 0.25

    Loss if the stop is hit: 100 ticks x $1.00 x 0.25 lots = $25.00, exactly
    the budget, because 0.25 lands on the lot grid.
    """

    volume = compute_volume(
        stop_distance=Decimal("0.00100"),
        book_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.25"),
        contract=_contract(),
    )

    assert volume == Decimal("0.25")


def test_volume_floors_to_the_lot_grid_and_never_rounds_up() -> None:
    """Same contract, an equity that does not divide evenly:

        budget        = 10_000 x 0.30 / 100          = 30.00
        money_per_lot = 100 x 1.00                   = 100.00
        raw volume    = 30.00 / 100.00               = 0.30
        ... now with a 0.10 lot step:
        on the grid   = floor(0.30 / 0.10) x 0.10    = 0.30

    and with an equity of 10_500 the raw volume is 0.315, which must floor to
    0.30 and not round to 0.32 or up to 0.40. Loss at the stop is then
    100 x 1.00 x 0.30 = $30.00 against a budget of $31.50 -- short by less
    than one lot step's worth, which is I-19's bound.
    """

    volume = compute_volume(
        stop_distance=Decimal("0.00100"),
        book_equity=Decimal("10500"),
        risk_per_trade_pct=Decimal("0.30"),
        contract=_contract(quantity_increment=Decimal("0.10"), quantity_min=Decimal("0.10")),
    )

    assert volume == Decimal("0.30")


def test_a_wider_stop_buys_a_smaller_position() -> None:
    """Doubling the stop distance must halve the volume, or the risk budget is
    not being respected at all."""

    narrow = compute_volume(
        stop_distance=Decimal("0.00100"),
        book_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.25"),
        contract=_contract(),
    )
    wide = compute_volume(
        stop_distance=Decimal("0.00200"),
        book_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.25"),
        contract=_contract(),
    )

    assert wide == narrow / 2


def test_volume_refuses_a_non_positive_stop_distance() -> None:
    with pytest.raises(ValueError, match="stop distance"):
        compute_volume(
            stop_distance=Decimal("0"),
            book_equity=Decimal("10000"),
            risk_per_trade_pct=Decimal("0.25"),
            contract=_contract(),
        )


def test_volume_refuses_non_positive_equity() -> None:
    """A blown account must not produce a number the caller could act on."""

    with pytest.raises(ValueError, match="equity"):
        compute_volume(
            stop_distance=Decimal("0.00100"),
            book_equity=Decimal("0"),
            risk_per_trade_pct=Decimal("0.25"),
            contract=_contract(),
        )
```

- [ ] **Step 10: Run and watch them fail, then write `compute_volume`**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk/test_sizing.py -q
```

Then append to `risk/sizing.py`:

```python
def compute_volume(
    *,
    stop_distance: Decimal,
    book_equity: Decimal,
    risk_per_trade_pct: Decimal,
    contract: InstrumentContract,
) -> Decimal:
    """Lots such that the loss at ``stop_distance`` does not exceed the budget.

    ``book_equity`` is the book's own slice of firm equity, not firm equity.
    The caller does that multiply; see the engine.
    """

    if stop_distance <= 0:
        raise ValueError("stop distance must be positive")
    if book_equity <= 0:
        raise ValueError("book equity must be positive")
    budget = book_equity * risk_per_trade_pct / Decimal(100)
    ticks = stop_distance / contract.price_increment
    money_per_lot = ticks * contract.value_per_price_increment
    if money_per_lot <= 0:
        raise ValueError("non-positive money per lot; instrument contract invalid")
    return quantise_down(budget / money_per_lot, contract.quantity_increment)
```

- [ ] **Step 11: Write the stop-price tests, then the function**

```python
def test_a_long_stop_sits_below_entry_and_a_short_stop_above() -> None:
    contract = _contract()

    assert stop_price(
        side=Side.BUY,
        entry_price_ref=Decimal("1.10000"),
        stop_distance=Decimal("0.00100"),
        contract=contract,
    ) == Decimal("1.09900")
    assert stop_price(
        side=Side.SELL,
        entry_price_ref=Decimal("1.10000"),
        stop_distance=Decimal("0.00100"),
        contract=contract,
    ) == Decimal("1.10100")


def test_the_stop_price_is_quantised_away_from_entry() -> None:
    """entry_price_ref is a strategy's reference and need not sit on the grid.
    1.100005 - 0.00100 = 1.099005; rounding toward entry would give 1.09901
    and narrow the distance the volume was computed from."""

    assert stop_price(
        side=Side.BUY,
        entry_price_ref=Decimal("1.100005"),
        stop_distance=Decimal("0.00100"),
        contract=_contract(),
    ) == Decimal("1.09900")
    assert stop_price(
        side=Side.SELL,
        entry_price_ref=Decimal("1.100005"),
        stop_distance=Decimal("0.00100"),
        contract=_contract(),
    ) == Decimal("1.10101")


def test_stop_price_refuses_a_long_stop_that_cannot_land_above_zero() -> None:
    """A distance at or beyond the entry price has no representable stop. The
    engine gates this before calling, so reaching here is a caller bug."""

    with pytest.raises(ValueError, match="stop price"):
        stop_price(
            side=Side.BUY,
            entry_price_ref=Decimal("0.00100"),
            stop_distance=Decimal("0.00100"),
            contract=_contract(),
        )
```

Then:

```python
def stop_price(
    *,
    side: Side,
    entry_price_ref: Decimal,
    stop_distance: Decimal,
    contract: InstrumentContract,
) -> Decimal:
    """The protective stop, anchored on the proposal's reference price.

    Anchoring on the reference rather than on a live tick is what makes the
    decision replayable: the same proposal and the same stored bars must give
    the same stop in a backtest a year later, when no live quote exists. The
    gap between the reference and the actual fill is slippage, and that
    belongs to execution.
    """

    if side is Side.BUY:
        raw = entry_price_ref - stop_distance
        if raw < contract.price_increment:
            raise ValueError("stop price would not land on a positive tick")
        return quantise_down(raw, contract.price_increment)
    return quantise_up(entry_price_ref + stop_distance, contract.price_increment)
```

- [ ] **Step 12: Run the full suite, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest -q
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/risk tests/unit/risk
git commit -m "feat: stop distance and volume arithmetic with one-sided rounding"
```

---

### Task 3: The engine — gates, decision, margin seam

**Files:**
- Create: `src/trading_house/risk/engine.py`
- Test: `tests/unit/risk/test_engine.py`

**Interfaces:**
- Consumes: everything Task 2 produced, plus `Constitution`, `BookLimits`, `ScalpLimits`, `SafeModeTriggers` from `constitution.models`, and `Clock` / `ensure_utc` from `core.clock`.
- Produces, for Task 4:
  ```python
  class RejectionReason(str, Enum)          # values listed below
  MARGIN_HEADROOM_MULTIPLE: Decimal          # Decimal(2)
  class MarginPort(Protocol):
      def free_margin(self) -> Decimal: ...
      def required_margin(self, *, instrument_id: str, side: Side,
                          quantity: Decimal, price: Decimal) -> Decimal: ...
  class RiskEngine:
      def __init__(self, constitution: Constitution, clock: Clock) -> None
      def evaluate(self, proposal: TradeProposal, *, contract: InstrumentContract,
                   firm_equity: Decimal, atr: Decimal, median_spread_points: Decimal,
                   tick_spread_points: Decimal, tick_time: datetime) -> RiskDecision
      def evaluate_for_execution(self, proposal: TradeProposal, *, margin: MarginPort,
                                 contract: InstrumentContract, firm_equity: Decimal,
                                 atr: Decimal, median_spread_points: Decimal,
                                 tick_spread_points: Decimal,
                                 tick_time: datetime) -> RiskDecision
  ```
  `evaluate_for_execution` repeats `evaluate`'s seven keywords explicitly. A
  `**facts: object` passthrough will not type-check under mypy strict.

**On `checks_passed`:** `BaseRiskDecision` carries both `reasons` and `checks_passed`. Both hold `RejectionReason` values. A name in `reasons` is a failure; the same name in `checks_passed` means that failure mode was checked and ruled out. This mirrors the master spec's §7.2 `_c(check, reasons, passed)` helper, which appends the same identifier to whichever list applies.

- [ ] **Step 1: Write the gate tests**

```python
# tests/unit/risk/test_engine.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.unit.risk.conftest import _contract
from trading_house.constitution.models import Constitution
from trading_house.core.clock import FixedClock
from trading_house.core.schemas import RejectedRiskDecision, Side, TradeProposal
from trading_house.core.values import AssetClass
from trading_house.risk.engine import RejectionReason, RiskEngine

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _proposal(**overrides: object) -> TradeProposal:
    fields: dict[str, object] = {
        "event_time": NOW,
        "availability_time": NOW,
        "processing_time": NOW,
        "source": "test",
        "proposal_id": "p-1",
        "strategy_id": "s-1",
        "strategy_version": "1",
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "side": Side.BUY,
        "horizon_seconds": 60,
        "entry_condition": "test",
        "entry_price_ref": Decimal("1.10000"),
        "invalidation_price": Decimal("1.09700"),
        "max_holding_seconds": 300,
        "expected_return_bps": 5.0,
        "expected_return_stdev_bps": 2.0,
        "expected_cost_bps": 1.0,
        "win_probability": 0.55,
        "calibration_id": "c-1",
        "required_liquidity": {"amount": Decimal("1"), "unit": "lots"},
        "regime_ref": "r-1",
        "features_snapshot_id": "f-1",
    }
    fields.update(overrides)
    return TradeProposal.model_validate(fields)


def _engine(constitution: Constitution) -> RiskEngine:
    return RiskEngine(constitution, FixedClock(NOW))


def _facts(**overrides: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "firm_equity": Decimal("100000"),
        "atr": Decimal("0.00050"),
        "median_spread_points": Decimal("10"),
        "tick_spread_points": Decimal("10"),
        "tick_time": NOW,
    }
    facts.update(overrides)
    return facts
```

`_contract` comes from Task 2's conftest — add
`from tests.unit.risk.conftest import _contract` to the imports above. Do not
define a second copy. `_proposal` and `_facts` are new here and belong in the
same conftest, so Task 4's tests can reach them too.

**There is no existing shared `constitution` fixture** — every test that needs one calls `load_constitution` with its own paths. Define it once in a new `tests/unit/risk/conftest.py`:

```python
# tests/unit/risk/conftest.py
"""The real signed constitution, so the gates are tested against the values
that will actually bind in production rather than against a fixture that can
drift away from them."""

from pathlib import Path

import pytest

from trading_house.constitution.loader import load_constitution
from trading_house.constitution.models import Constitution

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


@pytest.fixture(scope="session")
def constitution() -> Constitution:
    loaded = load_constitution(
        CONFIG_DIR / "risk_constitution.yaml",
        CONFIG_DIR / "risk_constitution.yaml.sig",
        CONFIG_DIR / "risk_constitution.public.pem",
    )
    return loaded.constitution
```

Every test below takes `constitution: Constitution` as its first parameter.

Then the gates, one test each:

```python
def test_an_unknown_book_is_rejected_before_anything_is_computed(constitution) -> None:
    decision = _engine(constitution).evaluate(
        _proposal(book="does_not_exist"), contract=_contract(), **_facts()
    )

    assert isinstance(decision, RejectedRiskDecision)
    assert RejectionReason.UNKNOWN_BOOK in decision.reasons


def test_a_contract_for_a_different_instrument_is_rejected(constitution) -> None:
    """Sizing against the wrong contract would use the wrong tick value and
    silently produce a position sized for a different instrument."""

    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(instrument_id="fx.gbpusd"), **_facts()
    )

    assert RejectionReason.INSTRUMENT_MISMATCH in decision.reasons


def test_a_long_proposal_on_a_short_only_symbol_is_rejected(constitution) -> None:
    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(can_open_long=False), **_facts()
    )

    assert RejectionReason.SIDE_NOT_PERMITTED in decision.reasons


def test_an_asset_class_outside_the_books_mandate_is_rejected(constitution) -> None:
    """fx_scalp declares asset_classes [fx, metal]. An equity CFD is not its
    business even when every other check passes."""

    decision = _engine(constitution).evaluate(
        _proposal(instrument_id="equity_cfd.aapl"),
        contract=_contract(instrument_id="equity_cfd.aapl", asset_class=AssetClass.EQUITY_CFD),
        **_facts(),
    )

    assert RejectionReason.ASSET_CLASS_NOT_PERMITTED in decision.reasons


def test_a_spread_above_the_ceiling_is_rejected(constitution) -> None:
    """fx_scalp is a scalp book: safe_mode_triggers.scalp allows 3.0x the
    median and ScalpLimits.max_spread_multiple_at_entry allows 1.5x. The
    tighter of the two binds, so 20 points against a 10-point median (2.0x)
    must be rejected even though it is inside the 3.0x safe-mode trigger."""

    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(), **_facts(tick_spread_points=Decimal("20"))
    )

    assert RejectionReason.SPREAD_EXCEEDS_CEILING in decision.reasons


def test_a_swing_book_uses_only_the_safe_mode_multiple(constitution) -> None:
    """SwingLimits has no entry multiple, so 2.0x must pass on fx_swing and
    fail on fx_scalp. If this passes on both, the scalp branch is dead."""

    decision = _engine(constitution).evaluate(
        _proposal(book="fx_swing"), contract=_contract(),
        **_facts(tick_spread_points=Decimal("20")),
    )

    assert RejectionReason.SPREAD_EXCEEDS_CEILING not in decision.reasons


def test_a_zero_median_spread_disables_the_ratio_gate(constitution) -> None:
    """A raw-spread account can genuinely report a zero median. Comparing
    against 3.0 x 0 would reject every trade on that account."""

    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(),
        **_facts(median_spread_points=Decimal("0"), tick_spread_points=Decimal("2")),
    )

    assert RejectionReason.SPREAD_EXCEEDS_CEILING not in decision.reasons


def test_a_stale_tick_is_rejected(constitution) -> None:
    """safe_mode_triggers.scalp allows 2 seconds."""

    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(),
        **_facts(tick_time=NOW - timedelta(seconds=3)),
    )

    assert RejectionReason.TICK_STALE in decision.reasons


def test_a_tick_inside_the_age_limit_passes(constitution) -> None:
    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(),
        **_facts(tick_time=NOW - timedelta(seconds=1)),
    )

    assert RejectionReason.TICK_STALE not in decision.reasons


def test_every_failing_gate_contributes_its_own_reason(constitution) -> None:
    """One rejection listing three faults is one diagnosis; three sequential
    rejections are three round trips."""

    decision = _engine(constitution).evaluate(
        _proposal(),
        contract=_contract(can_open_long=False),
        **_facts(tick_spread_points=Decimal("99"), tick_time=NOW - timedelta(seconds=30)),
    )

    assert RejectionReason.SIDE_NOT_PERMITTED in decision.reasons
    assert RejectionReason.SPREAD_EXCEEDS_CEILING in decision.reasons
    assert RejectionReason.TICK_STALE in decision.reasons
```

- [ ] **Step 2: Write the sizing-outcome tests**

```python
def test_an_approved_decision_carries_the_realised_risk_not_the_budget(constitution) -> None:
    """fx_scalp: capital_fraction 0.30, risk_per_trade_pct 0.25.

        book_equity   = 100_000 x 0.30              = 30_000
        budget        = 30_000 x 0.25 / 100         = 75.00
        distance      = max(2.0x? no -- k_sigma 1.2 x 0.00050 = 0.00060,
                            cost 2.0 x 10 x 0.00001 = 0.00020,
                            structural 0.00300,
                            floor 0.00001)          = 0.00300
        ticks         = 300, money_per_lot          = 300.00
        raw volume    = 75.00 / 300.00              = 0.25
        risk_money    = 300 x 1.00 x 0.25           = 75.00

    risk_money must be 75.00 -- what will actually be lost -- and not the
    budget by construction.
    """

    decision = _engine(constitution).evaluate(_proposal(), contract=_contract(), **_facts())

    assert decision.verdict == "APPROVED"
    assert decision.approved_quantity.amount == Decimal("0.25")
    assert decision.stop_loss_price == Decimal("1.09700")
    assert decision.risk_money == Decimal("75.00")


def test_book_equity_is_the_books_slice_not_firm_equity(constitution) -> None:
    """sleeve holds capital_fraction 0.10. Passing firm equity straight in
    would size this position ten times too large."""

    decision = _engine(constitution).evaluate(
        _proposal(book="sleeve"), contract=_contract(), **_facts()
    )

    # book_equity = 100_000 x 0.10 = 10_000; budget = 10_000 x 1.5 / 100 = 150.00
    # ticks 300, money_per_lot 300.00, volume floor(0.5 / 0.01) x 0.01 = 0.50
    assert decision.approved_quantity.amount == Decimal("0.50")


def test_a_position_below_the_minimum_lot_is_rejected_not_rounded_up(constitution) -> None:
    """Rounding a sub-minimum position up to quantity_min would take more risk
    than the constitution allows, on the trades where the budget was smallest."""

    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(quantity_min=Decimal("5"),
                                        quantity_increment=Decimal("5")),
        **_facts(),
    )

    assert RejectionReason.BELOW_MIN_LOT in decision.reasons
    assert decision.approved_quantity.amount == Decimal("0")
    assert decision.risk_money == Decimal("0")


def test_the_minimum_lot_boundary_admits_as_well_as_refuses(constitution) -> None:
    """The raw volume for these facts is exactly 0.25, so a minimum of 0.25
    must be approved and 0.26 must not. Without the approving half, an engine
    that rejected every proposal would pass the test above."""

    at_the_boundary = _engine(constitution).evaluate(
        _proposal(), contract=_contract(quantity_min=Decimal("0.25")), **_facts()
    )
    one_step_beyond = _engine(constitution).evaluate(
        _proposal(), contract=_contract(quantity_min=Decimal("0.26")), **_facts()
    )

    assert at_the_boundary.verdict == "APPROVED"
    assert at_the_boundary.approved_quantity.amount == Decimal("0.25")
    assert RejectionReason.BELOW_MIN_LOT in one_step_beyond.reasons


def test_a_position_above_the_maximum_lot_is_clamped_and_marked_resized(
    constitution,
) -> None:
    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(quantity_max=Decimal("0.10")), **_facts()
    )

    assert decision.verdict == "RESIZED"
    assert decision.approved_quantity.amount == Decimal("0.10")
    # risk_money follows the clamped size: 300 x 1.00 x 0.10 = 30.00
    assert decision.risk_money == Decimal("30.00")


def test_a_stop_that_cannot_land_above_zero_is_rejected(constitution) -> None:
    decision = _engine(constitution).evaluate(
        _proposal(entry_price_ref=Decimal("0.00200"),
                  invalidation_price=Decimal("0.00100")),
        contract=_contract(), **_facts(atr=Decimal("1")),
    )

    assert RejectionReason.STOP_PRICE_NOT_POSITIVE in decision.reasons
```

- [ ] **Step 3: Write the margin tests**

```python
class _Margin:
    def __init__(self, free: str, required: str) -> None:
        self._free = Decimal(free)
        self._required = Decimal(required)

    def free_margin(self) -> Decimal:
        return self._free

    def required_margin(self, *, instrument_id, side, quantity, price) -> Decimal:
        return self._required


def test_execution_path_rejects_when_headroom_is_below_two_times(constitution) -> None:
    """The master spec's 8.1 requires 2x headroom so that one adverse move
    cannot cascade into forced liquidation across the book."""

    decision = _engine(constitution).evaluate_for_execution(
        _proposal(), margin=_Margin(free="1900", required="1000"),
        contract=_contract(), **_facts(),
    )

    assert RejectionReason.INSUFFICIENT_FREE_MARGIN_HEADROOM in decision.reasons


def test_execution_path_approves_at_exactly_two_times(constitution) -> None:
    decision = _engine(constitution).evaluate_for_execution(
        _proposal(), margin=_Margin(free="2000", required="1000"),
        contract=_contract(), **_facts(),
    )

    assert decision.verdict == "APPROVED"


def test_execution_path_does_not_consult_margin_for_an_already_rejected_proposal(
    constitution,
) -> None:
    """A rejected proposal has no quantity to price, so calling the port would
    be asking the broker about a position that will never exist."""

    class _Exploding:
        def free_margin(self) -> Decimal:
            raise AssertionError("margin must not be consulted")

        def required_margin(self, **_: object) -> Decimal:
            raise AssertionError("margin must not be consulted")

    decision = _engine(constitution).evaluate_for_execution(
        _proposal(book="does_not_exist"), margin=_Exploding(),
        contract=_contract(), **_facts(),
    )

    assert RejectionReason.UNKNOWN_BOOK in decision.reasons
```

- [ ] **Step 4: Run and watch them fail**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk/test_engine.py -q
```

Expected: FAIL — `trading_house.risk.engine` does not exist.

- [ ] **Step 5: Write the engine**

```python
# src/trading_house/risk/engine.py
"""The deterministic risk gate and its one point of broker contact.

``evaluate`` is pure over its arguments: the same proposal and the same market
facts always yield the same decision, so a backtester replays it exactly.
``evaluate_for_execution`` adds the margin-headroom check, which needs a live
account and therefore cannot appear in the pure path.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol

from trading_house.constitution.models import BookLimits, Constitution, ScalpLimits
from trading_house.core.clock import Clock, ensure_utc
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import (
    ApprovedRiskDecision,
    RejectedRiskDecision,
    ResizedRiskDecision,
    RiskDecision,
    Side,
    TradeProposal,
)
from trading_house.core.values import PositiveQuantity, Quantity
from trading_house.risk.sizing import compute_stop_distance, compute_volume, stop_price

MARGIN_HEADROOM_MULTIPLE = Decimal(2)


class RejectionReason(str, Enum):  # noqa: UP042
    """Every way this engine can refuse.

    The same values appear in a decision's ``checks_passed``, where they mean
    the failure was checked for and ruled out.
    """

    UNKNOWN_BOOK = "unknown_book"
    INSTRUMENT_MISMATCH = "instrument_mismatch"
    NON_POSITIVE_EQUITY = "non_positive_equity"
    SIDE_NOT_PERMITTED = "side_not_permitted"
    ASSET_CLASS_NOT_PERMITTED = "asset_class_not_permitted"
    SPREAD_EXCEEDS_CEILING = "spread_exceeds_ceiling"
    TICK_STALE = "tick_stale"
    BELOW_MIN_LOT = "below_min_lot"
    STOP_PRICE_NOT_POSITIVE = "stop_price_not_positive"
    INSUFFICIENT_FREE_MARGIN_HEADROOM = "insufficient_free_margin_headroom"


class MarginPort(Protocol):
    """The narrowest surface the headroom rule needs. Two methods, no more."""

    def free_margin(self) -> Decimal: ...

    def required_margin(
        self, *, instrument_id: str, side: Side, quantity: Decimal, price: Decimal
    ) -> Decimal: ...
```

Then `RiskEngine`, in the same file:

```python
class RiskEngine:
    """The deterministic gate. Holds the constitution and a clock, nothing else."""

    def __init__(self, constitution: Constitution, clock: Clock) -> None:
        self._constitution = constitution
        self._clock = clock

    def evaluate(
        self,
        proposal: TradeProposal,
        *,
        contract: InstrumentContract,
        firm_equity: Decimal,
        atr: Decimal,
        median_spread_points: Decimal,
        tick_spread_points: Decimal,
        tick_time: datetime,
    ) -> RiskDecision:
        # The prelude returns immediately: without a book, a matching contract
        # or an equity, no later check has anything to compute against.
        book = self._constitution.books.get(proposal.book)
        if book is None:
            return self._reject(proposal, [RejectionReason.UNKNOWN_BOOK], [])
        if contract.instrument_id != proposal.instrument_id:
            return self._reject(proposal, [RejectionReason.INSTRUMENT_MISMATCH], [])
        if firm_equity <= 0:
            return self._reject(proposal, [RejectionReason.NON_POSITIVE_EQUITY], [])

        # The independent gates all run; each contributes its own reason.
        reasons: list[RejectionReason] = []
        passed: list[RejectionReason] = []
        _record(
            self._side_permitted(proposal.side, contract),
            RejectionReason.SIDE_NOT_PERMITTED, reasons, passed,
        )
        _record(
            contract.asset_class in book.asset_classes,
            RejectionReason.ASSET_CLASS_NOT_PERMITTED, reasons, passed,
        )
        _record(
            self._spread_within_ceiling(book, median_spread_points, tick_spread_points),
            RejectionReason.SPREAD_EXCEEDS_CEILING, reasons, passed,
        )
        _record(
            self._tick_fresh(book, tick_time),
            RejectionReason.TICK_STALE, reasons, passed,
        )
        if reasons:
            return self._reject(proposal, reasons, passed)

        distance = compute_stop_distance(
            atr=atr,
            median_spread_points=median_spread_points,
            entry_price_ref=proposal.entry_price_ref,
            invalidation_price=proposal.invalidation_price,
            contract=contract,
            k_sigma=book.k_sigma,
            k_spread=book.k_spread,
        )

        # Checked here rather than inside stop_price() so an unrepresentable
        # stop is a rejection the caller can read, not an exception to catch.
        if (
            proposal.side is Side.BUY
            and proposal.entry_price_ref - distance < contract.price_increment
        ):
            return self._reject(proposal, [RejectionReason.STOP_PRICE_NOT_POSITIVE], passed)
        passed.append(RejectionReason.STOP_PRICE_NOT_POSITIVE)

        # The book's own slice, not firm equity. Passing firm equity here would
        # over-risk the sleeve (capital_fraction 0.10) by ten times.
        book_equity = firm_equity * book.capital_fraction
        volume = compute_volume(
            stop_distance=distance,
            book_equity=book_equity,
            risk_per_trade_pct=book.risk_per_trade_pct,
            contract=contract,
        )
        if volume < contract.quantity_min:
            return self._reject(proposal, [RejectionReason.BELOW_MIN_LOT], passed)
        passed.append(RejectionReason.BELOW_MIN_LOT)

        resized = volume > contract.quantity_max
        if resized:
            volume = contract.quantity_max

        ticks = distance / contract.price_increment
        risk_money = ticks * contract.value_per_price_increment * volume
        return RISK_DECISION_ADAPTER.validate_python(
            {
                "verdict": "RESIZED" if resized else "APPROVED",
                "proposal_id": proposal.proposal_id,
                "reasons": (),
                "checks_passed": tuple(check.value for check in passed),
                "constitution_version": self._constitution.version,
                "approved_quantity": PositiveQuantity(amount=volume, unit="lots"),
                "stop_loss_price": stop_price(
                    side=proposal.side,
                    entry_price_ref=proposal.entry_price_ref,
                    stop_distance=distance,
                    contract=contract,
                ),
                "take_profit_price": None,
                "risk_money": risk_money,
                "risk_pct_of_book": risk_money / book_equity * Decimal(100),
            }
        )

    @staticmethod
    def _side_permitted(side: Side, contract: InstrumentContract) -> bool:
        return contract.can_open_long if side is Side.BUY else contract.can_open_short

    def _spread_within_ceiling(
        self, book: BookLimits, median_points: Decimal, tick_points: Decimal
    ) -> bool:
        """The tightest applicable ceiling binds.

        ``max_spread_multiple_of_median`` is defined for every horizon;
        ``ScalpLimits.max_spread_multiple_at_entry`` exists only for scalp and
        is entry-specific. Honouring one and ignoring the other would leave a
        signed ceiling with no enforcement anywhere.
        """

        if median_points <= 0:
            # A raw-spread account can genuinely report a zero median, and a
            # multiple of zero would reject every trade on it.
            return True
        triggers = self._constitution.safe_mode_triggers[book.horizon]
        ceiling = triggers.max_spread_multiple_of_median
        if isinstance(book.limits, ScalpLimits):
            ceiling = min(ceiling, book.limits.max_spread_multiple_at_entry)
        return tick_points <= ceiling * median_points

    def _tick_fresh(self, book: BookLimits, tick_time: datetime) -> bool:
        triggers = self._constitution.safe_mode_triggers[book.horizon]
        # float() builds a duration threshold here, not a money value.
        max_age = timedelta(seconds=float(triggers.max_tick_age_seconds))
        return self._clock.now() - ensure_utc(tick_time) <= max_age

    def _reject(
        self,
        proposal: TradeProposal,
        reasons: Sequence[RejectionReason],
        passed: Sequence[RejectionReason],
    ) -> RejectedRiskDecision:
        return RejectedRiskDecision(
            verdict="REJECTED",
            proposal_id=proposal.proposal_id,
            reasons=tuple(reason.value for reason in reasons),
            checks_passed=tuple(check.value for check in passed),
            constitution_version=self._constitution.version,
            approved_quantity=Quantity(amount=Decimal(0), unit="lots"),
            risk_money=Decimal(0),
            risk_pct_of_book=Decimal(0),
        )

    def evaluate_for_execution(
        self,
        proposal: TradeProposal,
        *,
        margin: MarginPort,
        contract: InstrumentContract,
        firm_equity: Decimal,
        atr: Decimal,
        median_spread_points: Decimal,
        tick_spread_points: Decimal,
        tick_time: datetime,
    ) -> RiskDecision:
        """``evaluate`` plus the master spec's 8.1 free-margin headroom rule.

        Two entry points rather than one so that forgetting the margin check is
        not a forgotten line but a differently named function -- and so the pure
        path stays callable from a backtester that has no terminal.
        """

        decision = self.evaluate(
            proposal,
            contract=contract,
            firm_equity=firm_equity,
            atr=atr,
            median_spread_points=median_spread_points,
            tick_spread_points=tick_spread_points,
            tick_time=tick_time,
        )
        if isinstance(decision, RejectedRiskDecision):
            return decision
        required = margin.required_margin(
            instrument_id=proposal.instrument_id,
            side=proposal.side,
            quantity=decision.approved_quantity.amount,
            price=proposal.entry_price_ref,
        )
        if required <= 0 or margin.free_margin() < required * MARGIN_HEADROOM_MULTIPLE:
            return self._reject(
                proposal, [RejectionReason.INSUFFICIENT_FREE_MARGIN_HEADROOM], []
            )
        return decision
```

and the module-level helper the gates use, placed above the class:

```python
def _record(
    ok: bool,
    reason: RejectionReason,
    reasons: list[RejectionReason],
    passed: list[RejectionReason],
) -> None:
    """Append the check's name to whichever list applies.

    This is the master spec's 7.2 ``_c(check, reasons, passed)`` helper: a name
    in ``reasons`` is a failure, and the same name in ``checks_passed`` means
    that failure was checked for and ruled out.
    """

    (passed if ok else reasons).append(reason)
```

Three notes on the code above:

- **`RISK_DECISION_ADAPTER` already exists** in `core/schemas.py` as a
  `TypeAdapter[RiskDecision]`. Using it gives one construction site for both
  executable verdicts instead of two near-identical constructor calls that
  could drift apart. Import it alongside the decision classes.
- Add `Sequence` to the `collections.abc` imports for `_reject`.
- `RejectionReason` values reach the schema through `.value`, because `reasons`
  and `checks_passed` are typed `tuple[NonEmptyStr, ...]`. Tests can still
  write `RejectionReason.UNKNOWN_BOOK in decision.reasons`, since a `str`-based
  enum compares equal to its own value.

- [ ] **Step 6: Run, iterate to green, then the whole suite**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/risk -q
UV_SYSTEM_CERTS=1 uv run pytest -q
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
```

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/risk/engine.py tests/unit/risk/test_engine.py
git commit -m "feat: risk engine gates, sizing decision and the margin seam"
```

---

### Task 4: Property, acceptance and documentation

**Files:**
- Create: `tests/property/test_risk.py`
- Create: `tests/acceptance/test_phase3.py`
- Modify: `tests/acceptance/test_architecture.py`
- Modify: `README.md`
- Modify: `mt5-multi-agent-trading-house-spec.md` (add I-19 to §0.2)

**Interfaces:**
- Consumes: everything Tasks 2 and 3 produced.
- Produces: nothing further.

- [ ] **Step 1: Write the I-19 property**

```python
# tests/property/test_risk.py
"""I-19: an approved decision's realised risk never exceeds the book's budget,
and falls short by less than one lot step's worth unless the lot cap bound it.

The master spec's 8.1 asks only that realised loss equal the budget "within one
tick value". That tolerance is symmetric, so it passes an implementation that
overshoots the budget on half its trades. This property is one-sided.
"""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from trading_house.risk.sizing import compute_volume

PRICE_INCREMENTS = st.sampled_from([Decimal("0.00001"), Decimal("0.001"), Decimal("0.01")])
LOT_STEPS = st.sampled_from([Decimal("0.01"), Decimal("0.1"), Decimal("1")])
TICK_VALUES = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100"), places=2)
EQUITIES = st.decimals(min_value=Decimal("1000"), max_value=Decimal("10000000"), places=2)
RISK_PCTS = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5"), places=2)
TICK_COUNTS = st.integers(min_value=1, max_value=10_000)


@given(
    price_increment=PRICE_INCREMENTS,
    lot_step=LOT_STEPS,
    tick_value=TICK_VALUES,
    equity=EQUITIES,
    risk_pct=RISK_PCTS,
    ticks=TICK_COUNTS,
)
def test_realised_risk_never_exceeds_the_budget(
    price_increment: Decimal,
    lot_step: Decimal,
    tick_value: Decimal,
    equity: Decimal,
    risk_pct: Decimal,
    ticks: int,
) -> None:
    contract = _contract(
        price_increment=price_increment,
        point_size=price_increment,
        quantity_increment=lot_step,
        quantity_min=lot_step,
        quantity_max=Decimal("1000000"),
        value_per_price_increment=tick_value,
        min_stop_distance=price_increment,
        freeze_distance=price_increment,
    )
    distance = price_increment * ticks

    volume = compute_volume(
        stop_distance=distance,
        book_equity=equity,
        risk_per_trade_pct=risk_pct,
        contract=contract,
    )

    budget = equity * risk_pct / Decimal(100)
    money_per_lot = ticks * tick_value
    realised = money_per_lot * volume

    assert realised <= budget
    assert budget - realised < money_per_lot * lot_step
```

Import the builder rather than redefining it:
`from tests.unit.risk.conftest import _contract`. This property overrides every
numeric field on the contract, so only the non-numeric boilerplate — instrument
id, currencies, financing, trade flags, fill policies — is shared, and none of
those can change `compute_volume`'s result.

- [ ] **Step 2: Prove the property can fail**

Temporarily change `quantise_down` to `quantise_up` inside `compute_volume` and run:

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/property/test_risk.py -q
```

Expected: FAIL on `realised <= budget`. Revert the change and confirm it passes again. **Record the shrunk counterexample in the commit message.** A property that has never been seen to fail is a property nobody has checked.

- [ ] **Step 3: Write the phase acceptance tests**

```python
# tests/acceptance/test_phase3.py
"""Phase 3 acceptance: sizing cannot exceed the budget and cannot approve a
position without a protective stop.

The general import rule lives in test_architecture.py; this file asserts what
Phase 3 itself promised.
"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RISK = PROJECT_ROOT / "src" / "trading_house" / "risk"


def test_no_risk_module_reaches_a_broker_or_the_store() -> None:
    """Sizing must run identically in live trading and in a backtester with no
    terminal. An import of brokers/ or marketdata/ would end that."""

    for path in sorted(RISK.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("trading_house.brokers", "trading_house.marketdata",
                          "trading_house.features"):
            assert forbidden not in source, f"{path.name} imports {forbidden}"


def test_the_sizing_module_holds_no_io() -> None:
    """risk/sizing.py is pure arithmetic. A Clock, a Protocol or a constitution
    reference in it means the boundary has moved."""

    source = (RISK / "sizing.py").read_text(encoding="utf-8")
    for forbidden in ("Clock", "Protocol", "Constitution"):
        assert forbidden not in source, f"sizing.py references {forbidden}"


def test_every_approved_decision_type_requires_a_stop_price() -> None:
    """The master spec's 7.3 forbids trading without a protective stop, and
    Phase 0 made it structural by typing stop_loss_price as non-optional on
    ExecutableRiskDecision. This test exists so a later widening to
    `Price | None` fails here rather than in production."""

    from trading_house.core.schemas import ExecutableRiskDecision

    annotation = ExecutableRiskDecision.model_fields["stop_loss_price"].annotation

    assert annotation is not None
    assert "None" not in str(annotation)


def test_the_engine_is_the_only_holder_of_the_constitution() -> None:
    """The guard above is only meaningful while something still holds it."""

    assert "Constitution" in (RISK / "engine.py").read_text(encoding="utf-8")
```

- [ ] **Step 4: Extend the architecture test**

In `tests/acceptance/test_architecture.py`, add beside the existing
`FEATURES_ROOT` / `FEATURES_FORBIDDEN` constants:

```python
RISK_ROOT = SOURCE_ROOT / "risk"
# risk/ may reach core/ and constitution/, nothing else this project owns. The
# same arithmetic has to produce the same answer in live trading and in a
# backtester with no terminal, and a broker or store import would end that.
RISK_FORBIDDEN = frozenset(
    {"trading_house.brokers", "trading_house.marketdata", "trading_house.features"}
)
```

and, beside the equivalent `features/` tests:

```python
def test_no_risk_module_imports_a_broker_store_or_feature_module() -> None:
    """A backtest whose sizing diverges from production lies about expectancy,
    so the risk arithmetic must not be able to reach live infrastructure."""

    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        if not path.is_relative_to(RISK_ROOT):
            continue
        reached = _reaches(tree, RISK_FORBIDDEN)
        if reached:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(reached)

    assert offenders == {}


def test_the_risk_package_is_not_empty() -> None:
    """Guard the guard: an empty risk/ would make the loop above pass
    vacuously, so a passing check reflects clean code rather than no code."""

    assert sorted(RISK_ROOT.rglob("*.py"))


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.brokers.mt5.gateway import Gateway",
        "import trading_house.marketdata",
        "from trading_house.features.engine import FeatureEngine",
    ],
)
def test_the_risk_import_guard_can_still_fail(statement: str) -> None:
    """Guard the guard: prove the detector flags a real import, so a passing
    check reflects risk/ staying clean rather than a detector gone blind."""

    assert _reaches(ast.parse(statement + "\n"), RISK_FORBIDDEN)
```

Note that `FEATURES_FORBIDDEN` already contains `trading_house.risk`, so the
existing features guard covers the reverse arrow and needs no change.

- [ ] **Step 5: Document**

Add a "Phase 3 — risk and sizing" section to `README.md` in the shape of the existing Phase 2 section, stating: the two entry points and why there are two; that `book_equity` is the book's slice of firm equity; and that the stop distance quantises up before the volume is computed from it.

Add I-19 to the invariant list in `mt5-multi-agent-trading-house-spec.md` §0.2, worded exactly as the spec's §4.4:

> **I-19** — An approved decision's `risk_money` never exceeds the book's budgeted risk, and — unless the lot cap bound it — falls short by less than one lot step's worth.

- [ ] **Step 6: Full gate, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run ruff format --check .
UV_SYSTEM_CERTS=1 uv run ruff check .
UV_SYSTEM_CERTS=1 uv run mypy
UV_SYSTEM_CERTS=1 uv run pytest -q
git add tests/ README.md mt5-multi-agent-trading-house-spec.md
git commit -m "test: pin I-19 and the risk import boundary"
```
