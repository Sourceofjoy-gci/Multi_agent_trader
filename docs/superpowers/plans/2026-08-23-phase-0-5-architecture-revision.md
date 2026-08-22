# Phase 0.5 Architecture Revision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the canonical contracts venue-neutral, add horizon-scoped books so scalping and swing coexist under separate risk budgets, admit equity CFDs, and define the boundaries for an autonomous strategy foundry and a fact/belief memory — all before Phase 1 hardens the MT5 gateway around MT5-shaped contracts.

**Architecture:** Split `core/schemas.py` into focused modules: `core/values.py` (primitive value types), `core/instruments.py` (venue-neutral instrument contract), `core/venue.py` (venue references and execution outcomes), `core/freezing.py` (deep-freeze helpers lifted out of `audit/models.py`), with `core/schemas.py` keeping only cross-module message contracts. Add `brokers/base.py`, `agents/providers/base.py`, `research/`, and `memory/` as protocol-and-schema-only packages. No implementation of any provider, foundry, or learned model lands in this phase.

**Tech Stack:** Python 3.12, Pydantic v2 (strict/frozen), `Decimal` for all quantities and prices, Ed25519 via `cryptography`, PostgreSQL 18 + Alembic, pytest + Hypothesis, Ruff, mypy strict, uv.

**Source spec:** `docs/superpowers/specs/2026-08-22-trading-house-architecture-revision-design.md`

## Global Constraints

- Phase 0.5 delivers **contracts and boundaries only**. Do not implement a broker adapter, an agent provider, the foundry loop, the sandbox container, or a learned cost model.
- Do not add `MetaTrader5`, `langgraph`, `openai`, `anthropic`, or `ccxt` as a dependency or import. `tests/acceptance/test_architecture.py` enforces this and must stay green.
- Python is exactly the 3.12 release line: `>=3.12,<3.13`.
- Every quantity and price is `Decimal`. Never `float`. Existing `float`-typed price and volume fields are migrated, not kept alongside.
- All distances (`min_stop_distance`, `freeze_distance`, `price_increment`) are expressed in **price units, not points**.
- Canonical models stay `strict=True`, `frozen=True`, `extra="forbid"`. `tests/property/test_schema_boundaries.py::test_every_canonical_subclass_is_strict_closed_and_frozen` enforces this.
- Every canonical `datetime` field keeps its UTC validator. Invariant I-10 is enforced by `tests/property/test_schema_boundaries.py::test_naive_datetimes_are_rejected_everywhere`.
- The risk constitution stays **venue-neutral**. MT5 facts (magic ranges, server symbols) live in `config/venue_binding.mt5.yaml`, which is signed with the same Ed25519 loader.
- Capital fractions across all declared books must sum to exactly `Decimal("1")`.
- Coverage gate is `--cov-fail-under=95`. The suite currently measures 96.73%; do not let a task land below the gate.
- Every task ends green on: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and the task's tests.
- Run `uv run ruff format .` before committing. Ruff excludes `*.md`; do not remove that exclusion.

## Invariants This Phase Adds

| # | Invariant | Task |
|---|---|---|
| I-11 | No process holding broker credentials may execute agent-authored code or shell commands | 11 |
| I-12 | Every strategy trial, including abandoned ones, is registered in the trial ledger before its results may inform any promotion decision | 12 |
| I-13 | Memory Store A is writable only by deterministic post-trade code. No agent may write a fact | 13, 14 |
| I-14 | Every memory read is point-in-time by `availability_time` | 13 |
| I-15 | Error recovery may not violate a constitution prohibition | 7 |
| I-16 | Correlation and leverage budgets are computed firm-wide across books, never per-book | 9 |

## File Structure

| File | Responsibility |
|---|---|
| `src/trading_house/core/values.py` | **New.** Primitive value types: `Quantity`, `PositiveQuantity`, `Price`, `BasisPoints`, `InstrumentId`, `BookId`, and the `AssetClass` / `Horizon` / `TimeInForce` / `IntentState` / `QuantityUnit` enums |
| `src/trading_house/core/freezing.py` | **New.** `FrozenDict`, `FrozenList`, `freeze_json` — lifted from `audit/models.py` so canonical schemas can use them too |
| `src/trading_house/core/instruments.py` | **New.** `InstrumentContract`, `FinancingModel`, `FillPolicy` |
| `src/trading_house/core/venue.py` | **New.** `Mt5VenueRef`, `VenueRef`, `RejectReason`, `RejectClass`, `REJECT_CLASS`, `ExecutionOutcome`, `PrecheckResult` |
| `src/trading_house/core/schemas.py` | **Modified.** Keeps only cross-module message contracts, now venue-neutral |
| `src/trading_house/audit/models.py` | **Modified.** Imports freeze helpers from `core.freezing` instead of defining them |
| `src/trading_house/brokers/base.py` | **New.** `BrokerAdapter` protocol, `MarketSnapshot`, `ReconciliationReport`, `VenueHealth` |
| `src/trading_house/constitution/models.py` | **Modified.** `Books` becomes a mapping; horizon-conditional limits; firm aggregates |
| `src/trading_house/constitution/binding.py` | **New.** `VenueBinding` model and loader |
| `src/trading_house/constitution/signing.py` | **Modified.** Generalised to `load_signed` over any artifact |
| `src/trading_house/agents/providers/base.py` | **New.** `AgentProvider` protocol, `AgentRun`, `RunBudget`, `ProviderCapabilities` |
| `src/trading_house/research/packages.py` | **New.** `StrategySpec`, `StrategyPackage` |
| `src/trading_house/research/trial_ledger.py` | **New.** `Trial`, `TrialOutcome`, `TrialLedger` protocol |
| `src/trading_house/memory/models.py` | **New.** Store A fact schemas, Store B belief schemas |
| `src/trading_house/memory/reader.py` | **New.** Point-in-time read API |
| `migrations/versions/0002_memory_and_trials.py` | **New.** Memory chains and trial ledger tables |
| `config/risk_constitution.yaml` | **Modified.** Horizon-scoped books; re-signed |
| `config/venue_binding.mt5.yaml` | **New.** Signed MT5 binding |

---

### Task 1: Core Value Types

**Files:**
- Create: `src/trading_house/core/values.py`
- Test: `tests/unit/core/test_values.py`

**Interfaces:**
- Produces: `CanonicalModel`, `NonEmptyStr`, `FiniteFloat`, `PositiveFiniteFloat`, `NonNegativeFiniteFloat`, `Probability` (all **moved here** from `core/schemas.py`), plus `QuantityUnit`, `AssetClass`, `Horizon`, `TimeInForce`, `IntentState`, `Price`, `BasisPoints`, `InstrumentId`, `BookId`, `Quantity`, `PositiveQuantity`. **Every later task imports these from `trading_house.core.values`, never from `core.schemas`.**
- Consumes: nothing.

> **Why the base model moves here.** `core/values.py` and `core/venue.py` need
> `CanonicalModel`, and `core/schemas.py` will need types from both. Leaving the
> base in `core/schemas.py` creates an import cycle at Task 5. Moving it now, in
> the task that creates the module, avoids the cycle ever existing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/core/test_values.py
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.core.values import (
    AssetClass,
    Horizon,
    PositiveQuantity,
    Quantity,
    TimeInForce,
)


def test_quantity_permits_zero() -> None:
    assert Quantity(amount=Decimal("0"), unit="lots").amount == Decimal("0")


def test_positive_quantity_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        PositiveQuantity(amount=Decimal("0"), unit="lots")


def test_quantity_rejects_float_under_strict_mode() -> None:
    with pytest.raises(ValidationError):
        Quantity(amount=0.1, unit="lots")  # type: ignore[arg-type]


def test_quantity_rejects_bool_amount() -> None:
    with pytest.raises(ValidationError):
        Quantity(amount=True, unit="lots")  # type: ignore[arg-type]


def test_quantity_rejects_unknown_unit() -> None:
    with pytest.raises(ValidationError):
        Quantity(amount=Decimal("1"), unit="bushels")  # type: ignore[arg-type]


def test_quantity_is_frozen() -> None:
    quantity = Quantity(amount=Decimal("1"), unit="lots")
    with pytest.raises(ValidationError):
        quantity.amount = Decimal("2")


def test_enums_use_stable_wire_values() -> None:
    assert AssetClass.EQUITY_CFD.value == "equity_cfd"
    assert Horizon.SCALP.value == "scalp"
    assert TimeInForce.GTC.value == "GTC"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/core/test_values.py -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.core.values'`.

- [ ] **Step 3: Implement the value types**

First **move** `CanonicalModel`, `NonEmptyStr`, `FiniteFloat`, `PositiveFiniteFloat`, `NonNegativeFiniteFloat` and `Probability` out of `src/trading_house/core/schemas.py` into the new module verbatim, then add the new types below them. In `core/schemas.py`, replace those definitions with:

```python
from trading_house.core.values import (
    CanonicalModel,
    FiniteFloat,
    NonEmptyStr,
    NonNegativeFiniteFloat,
    PositiveFiniteFloat,
    Probability,
)
```

`core/schemas.py` keeps re-exporting them, so existing imports elsewhere stay valid.

```python
# src/trading_house/core/values.py
"""Primitive value types shared by every canonical contract."""

from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class CanonicalModel(BaseModel):
    """Base model for canonical data exchanged between trading services."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


NonEmptyStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
PositiveFiniteFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegativeFiniteFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]

QuantityUnit = Literal["lots", "shares", "base_units", "contracts"]

Price = Annotated[Decimal, Field(gt=0)]
BasisPoints = Annotated[Decimal, Field(ge=0)]
InstrumentId = Annotated[str, StringConstraints(pattern=r"^[a-z]+\.[a-z0-9_]+$")]
BookId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


class AssetClass(str, Enum):  # noqa: UP042
    FX = "fx"
    METAL = "metal"
    EQUITY_CFD = "equity_cfd"


class Horizon(str, Enum):  # noqa: UP042
    SCALP = "scalp"
    SWING = "swing"


class TimeInForce(str, Enum):  # noqa: UP042
    GTC = "GTC"
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


class IntentState(str, Enum):  # noqa: UP042
    SUBMITTING = "SUBMITTING"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class Quantity(CanonicalModel):
    """A venue-neutral amount. Zero is permitted only where zero is a valid outcome."""

    amount: Annotated[Decimal, Field(ge=0)]
    unit: QuantityUnit

    @field_validator("amount", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid quantity")
        return value


class PositiveQuantity(Quantity):
    """The only quantity an executable intent or open position may carry."""

    amount: Annotated[Decimal, Field(gt=0)]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/core/test_values.py -q --no-cov`

Expected: PASS, 7 tests.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/core/values.py tests/unit/core/test_values.py
git commit -m "feat: add venue-neutral value types"
```

---

### Task 2: Deep-Freeze Helpers Move To Core

Closes the review finding that `frozen=True` did not prevent mutation of a contained `dict`: `RegimeAssessment.probabilities` was mutable.

**Files:**
- Create: `src/trading_house/core/freezing.py`
- Modify: `src/trading_house/audit/models.py` (delete `FrozenDict`, `FrozenList`, `_freeze_json`; import them instead)
- Test: `tests/unit/core/test_freezing.py`

**Interfaces:**
- Produces: `FrozenDict`, `FrozenList`, `freeze_json(value: JsonValue) -> JsonValue`.
- Consumes: nothing from Task 1.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/core/test_freezing.py
import pytest
from pydantic import ValidationError

from trading_house.core.freezing import FrozenDict, FrozenList, freeze_json


def test_frozen_dict_rejects_mutation() -> None:
    frozen = freeze_json({"a": 1})
    assert isinstance(frozen, FrozenDict)
    with pytest.raises(TypeError):
        frozen["a"] = 2


def test_frozen_list_rejects_mutation() -> None:
    frozen = freeze_json([1, 2])
    assert isinstance(frozen, FrozenList)
    with pytest.raises(TypeError):
        frozen.append(3)


def test_freezing_is_recursive() -> None:
    frozen = freeze_json({"outer": {"inner": [1]}})
    with pytest.raises(TypeError):
        frozen["outer"]["inner"].append(2)


def test_regime_probabilities_cannot_be_mutated() -> None:
    """The review finding: a frozen model still allowed dict mutation."""

    from datetime import UTC, datetime

    from trading_house.core.schemas import RegimeAssessment

    when = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    model = RegimeAssessment(
        event_time=when,
        availability_time=when,
        processing_time=when,
        source="test",
        symbol="EURUSD",
        volatility_state="normal",
        trend_state="up",
        liquidity_state="normal",
        probabilities={"up": 1.0},
        uncertainty=0.5,
    )

    with pytest.raises(TypeError):
        model.probabilities["injected"] = 99.0
    assert model.probabilities == {"up": 1.0}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/core/test_freezing.py -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.core.freezing'`.

- [ ] **Step 3: Create the module by moving the existing implementation**

Move `FrozenDict`, `FrozenList` and `_freeze_json` verbatim out of `src/trading_house/audit/models.py` into `src/trading_house/core/freezing.py`, renaming `_freeze_json` to the public `freeze_json`:

```python
# src/trading_house/core/freezing.py
"""Recursive immutability for JSON-shaped data crossing a model boundary."""

from typing import NoReturn, cast

from pydantic import JsonValue


class FrozenDict(dict[str, JsonValue]):
    """A dictionary that preserves the immutable model boundary recursively."""

    @staticmethod
    def _immutable(*_: object, **__: object) -> NoReturn:
        raise TypeError("value is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class FrozenList(list[JsonValue]):
    """A list that preserves the immutable model boundary recursively."""

    @staticmethod
    def _immutable(*_: object, **__: object) -> NoReturn:
        raise TypeError("value is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return cast(JsonValue, FrozenDict({key: freeze_json(item) for key, item in value.items()}))
    if isinstance(value, list):
        return cast(JsonValue, FrozenList([freeze_json(item) for item in value]))
    return value
```

In `src/trading_house/audit/models.py`, delete the three moved definitions and add:

```python
from trading_house.core.freezing import freeze_json
```

Then replace every `_freeze_json(` call in that file with `freeze_json(`.

- [ ] **Step 4: Apply the freeze to the mutable canonical field**

In `src/trading_house/core/schemas.py`, add to `RegimeAssessment`:

```python
    @field_validator("probabilities")
    @classmethod
    def freeze_probabilities(cls, value: dict[str, float]) -> dict[str, float]:
        return cast(dict[str, float], freeze_json(cast(JsonValue, value)))
```

Add `from typing import cast`, `from pydantic import JsonValue`, and `from trading_house.core.freezing import freeze_json` to the imports.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/core/test_freezing.py tests/unit/audit -q --no-cov`

Expected: PASS. The existing audit tests must stay green — they exercise the same helpers through their new home.

- [ ] **Step 6: Verify the static gates and the full unit suite**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest tests/unit tests/property -q --no-cov`

Expected: all exit `0`.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/core/freezing.py src/trading_house/core/schemas.py src/trading_house/audit/models.py tests/unit/core/test_freezing.py
git commit -m "fix: close the shallow-freeze hole in canonical models"
```

---

### Task 3: Venue-Neutral Instrument Contract

**Files:**
- Create: `src/trading_house/core/instruments.py`
- Test: `tests/unit/core/test_instruments.py`

**Interfaces:**
- Produces: `FillPolicy`, `FinancingModel`, `InstrumentContract`.
- Consumes: `AssetClass`, `InstrumentId`, `Price` from `trading_house.core.values`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/core/test_instruments.py
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.values import AssetClass

VALID: dict[str, object] = {
    "instrument_id": "fx.eurusd",
    "asset_class": AssetClass.FX,
    "base_currency": "EUR",
    "quote_currency": "USD",
    "price_increment": Decimal("0.00001"),
    "quantity_increment": Decimal("0.01"),
    "quantity_min": Decimal("0.01"),
    "quantity_max": Decimal("100"),
    "value_per_price_increment": Decimal("1"),
    "min_stop_distance": Decimal("0.0002"),
    "freeze_distance": Decimal("0.0001"),
    "session_calendar_id": "fx.24x5",
    "financing": FinancingModel.SWAP,
    "shortable": True,
    "supported_fills": frozenset({FillPolicy.IOC, FillPolicy.FOK}),
}


def test_valid_contract_builds() -> None:
    contract = InstrumentContract(**VALID)
    assert contract.asset_class is AssetClass.FX
    assert contract.price_increment == Decimal("0.00001")


def test_contract_is_frozen() -> None:
    contract = InstrumentContract(**VALID)
    with pytest.raises(ValidationError):
        contract.shortable = False


def test_quantity_bounds_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="quantity_max"):
        InstrumentContract(**{**VALID, "quantity_max": Decimal("0.001")})


def test_quantity_min_must_be_a_multiple_of_the_increment() -> None:
    with pytest.raises(ValidationError, match="quantity_increment"):
        InstrumentContract(**{**VALID, "quantity_min": Decimal("0.015")})


def test_at_least_one_fill_policy_is_required() -> None:
    with pytest.raises(ValidationError):
        InstrumentContract(**{**VALID, "supported_fills": frozenset()})


def test_equity_cfd_may_be_long_only() -> None:
    contract = InstrumentContract(
        **{
            **VALID,
            "instrument_id": "equity_cfd.aapl",
            "asset_class": AssetClass.EQUITY_CFD,
            "financing": FinancingModel.DIVIDEND_ADJUSTMENT,
            "shortable": False,
            "session_calendar_id": "xnas.regular",
        }
    )
    assert contract.shortable is False


def test_distances_are_rejected_when_not_positive() -> None:
    with pytest.raises(ValidationError):
        InstrumentContract(**{**VALID, "min_stop_distance": Decimal("0")})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/core/test_instruments.py -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.core.instruments'`.

- [ ] **Step 3: Implement the instrument contract**

```python
# src/trading_house/core/instruments.py
"""The venue-neutral description of a tradable instrument.

Every distance is in price units. Points are an MT5 encoding and are the
adapter's business, not the contract's.
"""

from decimal import Decimal
from enum import Enum
from typing import Annotated, Self

from pydantic import Field, model_validator

from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.core.values import AssetClass, InstrumentId

PositiveDecimal = Annotated[Decimal, Field(gt=0)]


class FillPolicy(str, Enum):  # noqa: UP042
    IOC = "IOC"
    FOK = "FOK"
    RETURN = "RETURN"


class FinancingModel(str, Enum):  # noqa: UP042
    SWAP = "swap"
    DIVIDEND_ADJUSTMENT = "dividend_adjustment"


class InstrumentContract(CanonicalModel):
    """What the venue guarantees about one instrument, in neutral terms."""

    instrument_id: InstrumentId
    asset_class: AssetClass
    base_currency: NonEmptyStr
    quote_currency: NonEmptyStr
    price_increment: PositiveDecimal
    quantity_increment: PositiveDecimal
    quantity_min: PositiveDecimal
    quantity_max: PositiveDecimal
    value_per_price_increment: PositiveDecimal
    min_stop_distance: PositiveDecimal
    freeze_distance: PositiveDecimal
    session_calendar_id: NonEmptyStr
    financing: FinancingModel
    shortable: bool
    supported_fills: frozenset[FillPolicy] = Field(min_length=1)

    @model_validator(mode="after")
    def quantity_bounds_are_coherent(self) -> Self:
        if self.quantity_max < self.quantity_min:
            raise ValueError("quantity_max must not be below quantity_min")
        if self.quantity_min % self.quantity_increment != 0:
            raise ValueError("quantity_min must be a multiple of quantity_increment")
        return self
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/core/test_instruments.py -q --no-cov`

Expected: PASS, 7 tests.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/core/instruments.py tests/unit/core/test_instruments.py
git commit -m "feat: describe instruments without venue encodings"
```

---

### Task 4: Venue References And The Error Taxonomy

Implements the three-class error taxonomy from spec §4.4 and makes I-15 testable.

**Files:**
- Create: `src/trading_house/core/venue.py`
- Test: `tests/unit/core/test_venue.py`

**Interfaces:**
- Produces: `Venue`, `Mt5VenueRef`, `VenueRef`, `RejectReason`, `RejectClass`, `REJECT_CLASS`, `ExecutionOutcome`, `PrecheckResult`.
- Consumes: `PositiveQuantity`, `Price` from `trading_house.core.values`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/core/test_venue.py
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.core.values import PositiveQuantity
from trading_house.core.venue import (
    REJECT_CLASS,
    ExecutionOutcome,
    Mt5VenueRef,
    PrecheckResult,
    RejectClass,
    RejectReason,
    Venue,
)

REF: dict[str, object] = {
    "venue": Venue.MT5,
    "magic": 110001,
    "server_symbol": "EURUSD.raw",
}


def test_venue_ref_builds_and_is_frozen() -> None:
    ref = Mt5VenueRef(**REF)
    assert ref.venue is Venue.MT5
    with pytest.raises(ValidationError):
        ref.magic = 2


def test_venue_ref_rejects_a_non_positive_magic() -> None:
    with pytest.raises(ValidationError):
        Mt5VenueRef(**{**REF, "magic": 0})


def test_every_reject_reason_has_exactly_one_class() -> None:
    """I-15 depends on this mapping being total: recovery branches on class."""

    assert set(REJECT_CLASS) == set(RejectReason)
    assert set(REJECT_CLASS.values()) <= set(RejectClass)


def test_contractual_rejections_are_classified_as_such() -> None:
    assert REJECT_CLASS[RejectReason.INVALID_STOPS] is RejectClass.CONTRACTUAL
    assert REJECT_CLASS[RejectReason.MARKET_CLOSED] is RejectClass.CONTRACTUAL


def test_authority_rejections_are_never_retryable() -> None:
    assert REJECT_CLASS[RejectReason.INSUFFICIENT_FUNDS] is RejectClass.AUTHORITY
    assert REJECT_CLASS[RejectReason.TRADE_DISABLED] is RejectClass.AUTHORITY


def test_a_filled_outcome_requires_a_fill_price_and_quantity() -> None:
    outcome = ExecutionOutcome(
        accepted=True,
        venue_ref=Mt5VenueRef(**REF),
        filled_quantity=PositiveQuantity(amount=Decimal("0.1"), unit="lots"),
        fill_price=Decimal("1.1"),
        reject_reason=None,
    )
    assert outcome.accepted is True


def test_an_accepted_outcome_cannot_carry_a_reject_reason() -> None:
    with pytest.raises(ValidationError, match="reject_reason"):
        ExecutionOutcome(
            accepted=True,
            venue_ref=Mt5VenueRef(**REF),
            filled_quantity=PositiveQuantity(amount=Decimal("0.1"), unit="lots"),
            fill_price=Decimal("1.1"),
            reject_reason=RejectReason.REQUOTE,
        )


def test_a_rejected_outcome_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="reject_reason"):
        ExecutionOutcome(
            accepted=False,
            venue_ref=None,
            filled_quantity=None,
            fill_price=None,
            reject_reason=None,
        )


def test_a_rejected_outcome_carries_no_fill() -> None:
    outcome = ExecutionOutcome(
        accepted=False,
        venue_ref=None,
        filled_quantity=None,
        fill_price=None,
        reject_reason=RejectReason.MARKET_CLOSED,
    )
    assert outcome.filled_quantity is None


def test_precheck_failure_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="reject_reason"):
        PrecheckResult(would_accept=False, reject_reason=None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/core/test_venue.py -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.core.venue'`.

- [ ] **Step 3: Implement the venue module**

```python
# src/trading_house/core/venue.py
"""Venue identity and the neutral outcome of asking a venue to act.

`RejectReason` is neutral. Raw broker return codes stay inside the venue
reference, where forensics can reach them and the risk engine cannot.
"""

from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveInt, model_validator

from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.core.values import PositiveQuantity


class Venue(str, Enum):  # noqa: UP042
    MT5 = "mt5"


class RejectClass(str, Enum):  # noqa: UP042
    """How recovery must respond. See spec section 4.4."""

    TRANSIENT = "transient"
    CONTRACTUAL = "contractual"
    AUTHORITY = "authority"


class RejectReason(str, Enum):  # noqa: UP042
    REQUOTE = "requote"
    PRICE_CHANGED = "price_changed"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    INVALID_STOPS = "invalid_stops"
    INVALID_QUANTITY = "invalid_quantity"
    MARKET_CLOSED = "market_closed"
    UNSUPPORTED_FILL = "unsupported_fill"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    TRADE_DISABLED = "trade_disabled"
    ACCOUNT_DISABLED = "account_disabled"


REJECT_CLASS: dict[RejectReason, RejectClass] = {
    RejectReason.REQUOTE: RejectClass.TRANSIENT,
    RejectReason.PRICE_CHANGED: RejectClass.TRANSIENT,
    RejectReason.TIMEOUT: RejectClass.TRANSIENT,
    RejectReason.DISCONNECTED: RejectClass.TRANSIENT,
    RejectReason.INVALID_STOPS: RejectClass.CONTRACTUAL,
    RejectReason.INVALID_QUANTITY: RejectClass.CONTRACTUAL,
    RejectReason.MARKET_CLOSED: RejectClass.CONTRACTUAL,
    RejectReason.UNSUPPORTED_FILL: RejectClass.CONTRACTUAL,
    RejectReason.INSUFFICIENT_FUNDS: RejectClass.AUTHORITY,
    RejectReason.TRADE_DISABLED: RejectClass.AUTHORITY,
    RejectReason.ACCOUNT_DISABLED: RejectClass.AUTHORITY,
}


class Mt5VenueRef(CanonicalModel):
    """Everything MT5-shaped about one submitted intent, in one place."""

    venue: Literal[Venue.MT5]
    magic: PositiveInt
    server_symbol: NonEmptyStr
    order_ticket: PositiveInt | None = None
    position_ticket: PositiveInt | None = None
    retcode: int | None = None


# One variant today. Pydantic needs a genuine union for a discriminator, so
# this stays a bare alias until a second venue exists, at which point it
# widens without any consumer changing:
#   VenueRef = Annotated[Mt5VenueRef | XVenueRef, Field(discriminator="venue")]
VenueRef = Mt5VenueRef


class ExecutionOutcome(CanonicalModel):
    """What the venue actually did. Never carries a raw broker message."""

    accepted: bool
    venue_ref: VenueRef | None
    filled_quantity: PositiveQuantity | None
    fill_price: Annotated[Decimal, Field(gt=0)] | None
    reject_reason: RejectReason | None

    @model_validator(mode="after")
    def outcome_is_internally_consistent(self) -> Self:
        if self.accepted and self.reject_reason is not None:
            raise ValueError("an accepted outcome cannot carry a reject_reason")
        if not self.accepted and self.reject_reason is None:
            raise ValueError("a rejected outcome requires a reject_reason")
        if not self.accepted and (self.filled_quantity is not None or self.fill_price is not None):
            raise ValueError("a rejected outcome cannot carry a fill")
        return self


class PrecheckResult(CanonicalModel):
    """The venue's opinion before the market is touched."""

    would_accept: bool
    reject_reason: RejectReason | None

    @model_validator(mode="after")
    def failure_is_explained(self) -> Self:
        if not self.would_accept and self.reject_reason is None:
            raise ValueError("a failed precheck requires a reject_reason")
        if self.would_accept and self.reject_reason is not None:
            raise ValueError("a passing precheck cannot carry a reject_reason")
        return self
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/core/test_venue.py -q --no-cov`

Expected: PASS, 10 tests.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/core/venue.py tests/unit/core/test_venue.py
git commit -m "feat: classify venue outcomes without broker encodings"
```

---

### Task 5: Rewrite OrderIntent Venue-Neutral

**Files:**
- Modify: `src/trading_house/core/schemas.py` (replace the `OrderIntent` class)
- Modify: `tests/unit/core/test_schemas_execution.py`
- Modify: `tests/property/test_schema_boundaries.py` (the `OrderIntent` builder)

**Interfaces:**
- Produces: the rewritten `OrderIntent`.
- Consumes: `BookId`, `InstrumentId`, `PositiveQuantity`, `Price`, `BasisPoints`, `TimeInForce`, `IntentState` (Task 1); `VenueRef`, `ExecutionOutcome` (Task 4); `Side` (already in `core/schemas.py`).

- [ ] **Step 1: Write the failing tests**

Replace the `valid_intent` fixture in `tests/unit/core/test_schemas_execution.py` with the neutral shape and add these tests:

```python
from decimal import Decimal

from trading_house.core.values import IntentState, PositiveQuantity, TimeInForce
from trading_house.core.venue import Mt5VenueRef, Venue


def _intent() -> dict[str, object]:
    from datetime import UTC, datetime

    return {
        "intent_id": "i-1",
        "proposal_id": "p-1",
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "side": Side.BUY,
        "quantity": PositiveQuantity(amount=Decimal("0.1"), unit="lots"),
        "stop_loss": Decimal("1.0950"),
        "take_profit": None,
        "time_in_force": TimeInForce.IOC,
        "max_slippage_bps": Decimal("2"),
        "state": IntentState.SUBMITTING,
        "t_submit_utc": datetime(2026, 8, 23, 9, 0, tzinfo=UTC),
        "venue_ref": None,
        "outcome": None,
    }


def test_order_intent_carries_no_venue_encoding() -> None:
    forbidden = {"magic", "deviation_points", "filling", "retcode",
                 "broker_order_ticket", "broker_position_ticket", "volume", "sl", "tp"}
    assert forbidden.isdisjoint(OrderIntent.model_fields)


def test_order_intent_builds_from_neutral_fields() -> None:
    intent = OrderIntent(**_intent())
    assert intent.quantity.amount == Decimal("0.1")
    assert intent.book == "fx_scalp"


def test_order_intent_rejects_a_zero_quantity() -> None:
    with pytest.raises(ValidationError):
        OrderIntent(**{**_intent(), "quantity": PositiveQuantity(amount=Decimal("0"), unit="lots")})


def test_order_intent_rejects_a_malformed_instrument_id() -> None:
    with pytest.raises(ValidationError):
        OrderIntent(**{**_intent(), "instrument_id": "EURUSD"})


def test_order_intent_accepts_a_venue_reference_once_submitted() -> None:
    intent = OrderIntent(
        **{
            **_intent(),
            "state": IntentState.CONFIRMED,
            "venue_ref": Mt5VenueRef(venue=Venue.MT5, magic=110001, server_symbol="EURUSD.raw"),
        }
    )
    assert intent.venue_ref is not None
    assert intent.venue_ref.magic == 110001


def test_order_intent_normalizes_submit_time_to_utc() -> None:
    from datetime import datetime, timedelta, timezone

    intent = OrderIntent(
        **{**_intent(), "t_submit_utc": datetime(2026, 8, 23, 11, 0, tzinfo=timezone(timedelta(hours=2)))}
    )
    assert intent.t_submit_utc.hour == 9


def test_order_intent_rejects_a_naive_submit_time() -> None:
    from datetime import datetime

    with pytest.raises(ValidationError):
        OrderIntent(**{**_intent(), "t_submit_utc": datetime(2026, 8, 23, 9, 0)})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/core/test_schemas_execution.py -q --no-cov`

Expected: FAIL — `OrderIntent` still declares `magic`, and `Extra inputs are not permitted` for the neutral fields.

- [ ] **Step 3: Replace the OrderIntent class**

In `src/trading_house/core/schemas.py`, delete the existing `OrderIntent` and add:

```python
class OrderIntent(CanonicalModel):
    """One idempotent request to change a position. Neutral by construction."""

    intent_id: NonEmptyStr
    proposal_id: NonEmptyStr
    book: BookId
    instrument_id: InstrumentId
    side: Side
    quantity: PositiveQuantity
    stop_loss: Price
    take_profit: Price | None
    time_in_force: TimeInForce
    max_slippage_bps: BasisPoints
    state: IntentState
    t_submit_utc: datetime
    venue_ref: VenueRef | None = None
    outcome: ExecutionOutcome | None = None

    @field_validator("t_submit_utc")
    @classmethod
    def normalize_submit_time(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error
```

Add the imports:

```python
from trading_house.core.values import (
    BasisPoints,
    BookId,
    InstrumentId,
    IntentState,
    PositiveQuantity,
    Price,
    TimeInForce,
)
from trading_house.core.venue import ExecutionOutcome, VenueRef
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/core tests/property -q --no-cov`

Expected: PASS. Update the `OrderIntent` builder in `tests/property/test_schema_boundaries.py` to the neutral shape so the boundary properties keep covering it.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/core tests/unit/core tests/property
git commit -m "feat: make order intents venue-neutral"
```

---

### Task 6: Migrate Proposals, Decisions, And Positions To Quantity

**Files:**
- Modify: `src/trading_house/core/schemas.py` (`TradeProposal`, `BaseRiskDecision` family, `PositionState`)
- Modify: `tests/unit/core/test_schemas_market.py`, `tests/unit/core/test_schemas_execution.py`, `tests/property/test_schema_boundaries.py`

**Interfaces:**
- Produces: `TradeProposal`, `ApprovedRiskDecision`, `ResizedRiskDecision`, `RejectedRiskDecision`, `RISK_DECISION_ADAPTER`, `PositionState`, all quantity- and book-neutral.
- Consumes: `BookId`, `InstrumentId`, `Price`, `Quantity`, `PositiveQuantity` (Task 1); `VenueRef` (Task 4).

- [ ] **Step 1: Write the failing tests**

```python
def test_rejected_decision_carries_a_zero_quantity_not_a_literal() -> None:
    decision = RejectedRiskDecision(
        proposal_id="p-1",
        reasons=("daily_loss_stop",),
        checks_passed=(),
        constitution_version=1,
        verdict="REJECTED",
        approved_quantity=Quantity(amount=Decimal("0"), unit="lots"),
        stop_loss_price=None,
        take_profit_price=None,
        risk_money=Decimal("0"),
        risk_pct_of_book=Decimal("0"),
    )
    assert decision.approved_quantity.amount == Decimal("0")


def test_an_executable_decision_cannot_carry_a_zero_quantity() -> None:
    """PositiveQuantity makes a zero-volume live order unrepresentable."""

    with pytest.raises(ValidationError):
        ApprovedRiskDecision(
            proposal_id="p-1",
            reasons=(),
            checks_passed=("all",),
            constitution_version=1,
            verdict="APPROVED",
            approved_quantity=PositiveQuantity(amount=Decimal("0"), unit="lots"),
            stop_loss_price=Decimal("1.09"),
            take_profit_price=None,
            risk_money=Decimal("50"),
            risk_pct_of_book=Decimal("0.25"),
        )


def test_no_canonical_model_still_uses_lot_denominated_floats() -> None:
    forbidden = {"approved_volume_lots", "required_liquidity_lots", "volume"}
    for model in (TradeProposal, ApprovedRiskDecision, RejectedRiskDecision, PositionState):
        assert forbidden.isdisjoint(model.model_fields), model.__name__


def test_position_state_records_its_book_and_venue_reference() -> None:
    assert "book" in PositionState.model_fields
    assert "venue_ref" in PositionState.model_fields
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/core/test_schemas_execution.py -q --no-cov`

Expected: FAIL — `approved_volume_lots` still present, `approved_quantity` unknown.

- [ ] **Step 3: Apply the migration**

In `src/trading_house/core/schemas.py`:

- `TradeProposal`: `book: Book` becomes `book: BookId`; `symbol: NonEmptyStr` becomes `instrument_id: InstrumentId`; `entry_price_ref` and `invalidation_price` become `Price`; `required_liquidity_lots: PositiveFiniteFloat` becomes `required_liquidity: PositiveQuantity`.
- `ExecutableRiskDecision`: `approved_volume_lots` becomes `approved_quantity: PositiveQuantity`; `stop_loss_price` and `take_profit_price` become `Price` / `Price | None`; `risk_money` and `risk_pct_of_book` become `Annotated[Decimal, Field(gt=0)]`.
- `RejectedRiskDecision`: `approved_quantity: Quantity` (zero-permitting), `risk_money: Literal[Decimal("0")]` replaced by `Annotated[Decimal, Field(le=0, ge=0)]`, same for `risk_pct_of_book`. Delete the `zero_fields_are_not_booleans` validator — `Quantity` already rejects booleans and `Decimal` fields reject `bool` under strict mode. Keep `has_rejection_reason`.
- `PositionState`: `book: Book` becomes `book: BookId`; `symbol` becomes `instrument_id: InstrumentId`; `volume` becomes `quantity: PositiveQuantity`; `open_price`, `current_sl`, `current_tp`, `initial_risk_distance` become `Price`; add `venue_ref: VenueRef | None = None`.
- Delete the `Book` enum entirely.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/property -q --no-cov`

Expected: PASS. Update every builder in `tests/property/test_schema_boundaries.py` and the market/execution schema tests to the new shapes.

- [ ] **Step 5: Verify the static gates and coverage**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest tests/unit tests/property --no-cov -q`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/core tests
git commit -m "feat: denominate risk decisions in decimal quantities"
```

---

### Task 7: The BrokerAdapter Protocol And Recovery Policy

**Files:**
- Create: `src/trading_house/brokers/__init__.py`, `src/trading_house/brokers/base.py`
- Modify: `src/trading_house/core/venue.py` (add `RecoveryAction` and `recovery_for`)
- Test: `tests/unit/brokers/test_base.py`

**Interfaces:**
- Produces: `MarketSnapshot`, `Quote`, `VenueHealth`, `ReconciliationReport`, `BrokerAdapter`; `RecoveryAction`, `recovery_for(reason: RejectReason) -> RecoveryAction`.
- Consumes: everything from Tasks 1, 3, 4, 5.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/brokers/test_base.py
import inspect

from trading_house.brokers.base import BrokerAdapter
from trading_house.core.venue import RecoveryAction, RejectReason, recovery_for

EXPECTED_METHODS = {
    "describe_instrument", "snapshot", "precheck", "submit",
    "amend_protection", "close", "reconcile", "health",
}


def test_adapter_surface_is_exactly_eight_methods() -> None:
    """A wide adapter is a leaky adapter. Adding a ninth needs a design change."""

    actual = {name for name in vars(BrokerAdapter) if not name.startswith("_")}
    assert actual == EXPECTED_METHODS


def test_adapter_is_a_runtime_checkable_protocol() -> None:
    assert getattr(BrokerAdapter, "_is_protocol", False) is True


def test_every_reject_reason_has_a_recovery_action() -> None:
    for reason in RejectReason:
        assert isinstance(recovery_for(reason), RecoveryAction)


def test_recovery_never_widens_a_stop() -> None:
    """I-15: recovery may not violate the constitution's stop_widening prohibition."""

    assert not any("WIDEN" in action.name for action in RecoveryAction)


def test_invalid_stops_refreshes_the_contract_rather_than_retrying() -> None:
    assert recovery_for(RejectReason.INVALID_STOPS) is RecoveryAction.REFRESH_CONTRACT_AND_RESIZE


def test_authority_failures_enter_safe_mode() -> None:
    assert recovery_for(RejectReason.INSUFFICIENT_FUNDS) is RecoveryAction.ENTER_SAFE_MODE
    assert recovery_for(RejectReason.TRADE_DISABLED) is RecoveryAction.ENTER_SAFE_MODE


def test_transient_failures_retry_with_a_fresh_price() -> None:
    assert recovery_for(RejectReason.REQUOTE) is RecoveryAction.RETRY_WITH_FRESH_PRICE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/brokers/test_base.py -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.brokers'`.

- [ ] **Step 3: Add the recovery policy to core/venue.py**

```python
class RecoveryAction(str, Enum):
    """The only three responses to a venue rejection.

    There is deliberately no widen-the-stop action: the constitution forbids
    stop widening, and recovery may not breach a prohibition (I-15).
    """

    RETRY_WITH_FRESH_PRICE = "retry_with_fresh_price"
    REFRESH_CONTRACT_AND_RESIZE = "refresh_contract_and_resize"
    ENTER_SAFE_MODE = "enter_safe_mode"


_RECOVERY: dict[RejectClass, RecoveryAction] = {
    RejectClass.TRANSIENT: RecoveryAction.RETRY_WITH_FRESH_PRICE,
    RejectClass.CONTRACTUAL: RecoveryAction.REFRESH_CONTRACT_AND_RESIZE,
    RejectClass.AUTHORITY: RecoveryAction.ENTER_SAFE_MODE,
}


def recovery_for(reason: RejectReason) -> RecoveryAction:
    """Map a rejection to its only permitted recovery."""

    return _RECOVERY[REJECT_CLASS[reason]]
```

- [ ] **Step 4: Implement the adapter protocol**

```python
# src/trading_house/brokers/base.py
"""The complete surface a venue must present. Eight methods, nothing more."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import OrderIntent, PositionState
from trading_house.core.values import (
    BookId,
    CanonicalModel,
    InstrumentId,
    PositiveQuantity,
    Price,
)
from trading_house.core.venue import ExecutionOutcome, PrecheckResult, VenueRef


class Quote(CanonicalModel):
    instrument_id: InstrumentId
    bid: Price
    ask: Price
    observed_at: datetime


class MarketSnapshot(CanonicalModel):
    quotes: tuple[Quote, ...]
    taken_at: datetime


class VenueHealth(CanonicalModel):
    connected: bool
    server_utc_offset_seconds: int
    last_quote_age_seconds: int


class ReconciliationReport(CanonicalModel):
    book: BookId
    positions: tuple[PositionState, ...]
    unmatched_venue_refs: tuple[VenueRef, ...]
    reconciled_at: datetime


@runtime_checkable
class BrokerAdapter(Protocol):
    """Retcodes, fill negotiation, magic allocation and terminal lifecycle
    all live behind this surface, never in front of it."""

    def describe_instrument(self, instrument_id: InstrumentId) -> InstrumentContract: ...
    def snapshot(self, instrument_ids: Sequence[InstrumentId]) -> MarketSnapshot: ...
    def precheck(self, intent: OrderIntent) -> PrecheckResult: ...
    def submit(self, intent: OrderIntent) -> ExecutionOutcome: ...
    def amend_protection(
        self, ref: VenueRef, stop_loss: Price, take_profit: Price | None
    ) -> ExecutionOutcome: ...
    def close(self, ref: VenueRef, quantity: PositiveQuantity | None) -> ExecutionOutcome: ...
    def reconcile(self, book: BookId) -> ReconciliationReport: ...
    def health(self) -> VenueHealth: ...
```

Create `src/trading_house/brokers/__init__.py` exporting `BrokerAdapter`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/brokers -q --no-cov`

Expected: PASS, 7 tests.

- [ ] **Step 6: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/brokers src/trading_house/core/venue.py tests/unit/brokers
git commit -m "feat: define the broker adapter surface and recovery policy"
```

---

### Task 8: Books Become A Declared Map With Horizon Limits

**Files:**
- Modify: `src/trading_house/constitution/models.py`
- Modify: `tests/unit/constitution/test_models.py`

**Interfaces:**
- Produces: `ScalpLimits`, `SwingLimits`, `HorizonLimits`, `BookLimits` (with `horizon`, `asset_classes`, `limits`), `Constitution.books: Mapping[BookId, BookLimits]`.
- Consumes: `AssetClass`, `BookId`, `Horizon` (Task 1).

- [ ] **Step 1: Write the failing tests**

```python
def test_books_may_be_declared_freely() -> None:
    constitution = parse_constitution_yaml(CONSTITUTION_BYTES)
    assert set(constitution.books) == {"fx_scalp", "fx_swing", "equity_swing", "sleeve"}


def test_capital_fractions_must_sum_to_exactly_one() -> None:
    source = CONSTITUTION_BYTES.replace(b"capital_fraction: 0.30", b"capital_fraction: 0.31")
    with pytest.raises(ConfigurationError):
        parse_constitution_yaml(source)


def test_a_scalp_book_carries_scalp_limits() -> None:
    book = parse_constitution_yaml(CONSTITUTION_BYTES).books["fx_scalp"]
    assert book.horizon is Horizon.SCALP
    assert book.limits.max_orders_per_minute > 0
    assert book.limits.flat_by_session_close is True


def test_a_swing_book_carries_swing_limits() -> None:
    book = parse_constitution_yaml(CONSTITUTION_BYTES).books["fx_swing"]
    assert book.horizon is Horizon.SWING
    assert book.limits.max_weekend_exposure_pct > 0


def test_horizon_limits_do_not_cross_apply() -> None:
    """A swing limit silently applied to a scalp book is the failure this prevents."""

    scalp = parse_constitution_yaml(CONSTITUTION_BYTES).books["fx_scalp"].limits
    assert not hasattr(scalp, "max_weekend_exposure_pct")


def test_a_book_declares_which_asset_classes_it_may_trade() -> None:
    book = parse_constitution_yaml(CONSTITUTION_BYTES).books["equity_swing"]
    assert book.asset_classes == (AssetClass.EQUITY_CFD,)


def test_books_remain_frozen_and_closed() -> None:
    constitution = parse_constitution_yaml(CONSTITUTION_BYTES)
    with pytest.raises(ValidationError):
        constitution.books["fx_scalp"].capital_fraction = Decimal("0.5")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/constitution/test_models.py -q --no-cov`

Expected: FAIL — `Constitution.books` still has fixed `core` and `sleeve` fields.

- [ ] **Step 3: Implement the horizon-scoped book model**

```python
class ScalpLimits(ConstitutionModel):
    horizon: Literal[Horizon.SCALP]
    max_orders_per_minute: PositiveInt
    max_spread_multiple_at_entry: PositiveDecimal
    min_expected_edge_after_cost_bps: PositiveDecimal
    max_position_duration_seconds: PositiveInt
    flat_by_session_close: Literal[True]


class SwingLimits(ConstitutionModel):
    horizon: Literal[Horizon.SWING]
    max_overnight_positions: PositiveInt
    max_weekend_exposure_pct: Percentage
    max_swap_cost_pct_of_expected_edge: Percentage
    gap_risk_multiple: PositiveDecimal
    earnings_blackout_days: NonNegativeInt


HorizonLimits = Annotated[ScalpLimits | SwingLimits, Field(discriminator="horizon")]


class BookLimits(ConstitutionModel):
    capital_fraction: PositiveDecimal
    horizon: Horizon
    asset_classes: tuple[AssetClass, ...] = Field(min_length=1)
    risk_per_trade_pct: Percentage
    max_concurrent_positions: PositiveInt
    daily_loss_stop_pct: Percentage
    max_drawdown_halt_pct: Percentage
    max_gross_leverage: PositiveDecimal
    limits: HorizonLimits

    @model_validator(mode="after")
    def limits_match_the_declared_horizon(self) -> Self:
        if self.limits.horizon is not self.horizon:
            raise ValueError("horizon limits must match the book's declared horizon")
        return self
```

Change `Constitution.books` to `Mapping[BookId, BookLimits]` with `Field(min_length=1)`, delete the `Books` class, and rewrite the sum validator:

```python
    @model_validator(mode="after")
    def capital_fractions_sum_to_one(self) -> Self:
        total = sum((book.capital_fraction for book in self.books.values()), Decimal(0))
        if total != Decimal("1"):
            raise ValueError("book capital fractions must sum exactly to 1")
        return self
```

Apply `_integer_to_decimal` in a `mode="before"` validator to every new `Decimal` field, matching the existing pattern.

- [ ] **Step 4: Rewrite the constitution YAML**

Replace the `books:` block of `config/risk_constitution.yaml` with the four books from spec §3.1, each carrying its `horizon`, `asset_classes`, and nested `limits`. Fractions: `fx_scalp` 0.30, `fx_swing` 0.45, `equity_swing` 0.15, `sleeve` 0.10.

The committed signature is now stale and every signature test will fail. That is expected and is fixed in Task 15; until then run constitution tests with `parse_constitution_yaml` rather than `load_constitution`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/constitution/test_models.py -q --no-cov`

Expected: PASS.

- [ ] **Step 6: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/constitution/models.py config/risk_constitution.yaml tests/unit/constitution/test_models.py
git commit -m "feat: scope books by horizon and asset class"
```

---

### Task 9: Firm Aggregates And Per-Horizon Safe Mode

Makes I-16 executable.

**Files:**
- Modify: `src/trading_house/constitution/models.py`, `config/risk_constitution.yaml`
- Modify: `tests/unit/constitution/test_models.py`
- Create: `tests/acceptance/test_risk_authority.py`

**Interfaces:**
- Produces: `FirmLimits` with `max_aggregate_open_risk_pct`, `max_gross_leverage`, `max_correlated_cluster_risk_pct`, `max_single_instrument_risk_pct`; `SafeModeTriggers` keyed by `Horizon`.
- Consumes: Task 8's models.

- [ ] **Step 1: Write the failing tests**

```python
# tests/acceptance/test_risk_authority.py
from trading_house.constitution.models import BookLimits, FirmLimits

FIRM_ONLY_LIMITS = {
    "max_aggregate_open_risk_pct",
    "max_gross_leverage",
    "max_correlated_cluster_risk_pct",
    "max_single_instrument_risk_pct",
}


def test_firm_declares_every_cross_book_budget() -> None:
    assert FIRM_ONLY_LIMITS <= set(FirmLimits.model_fields)


def test_no_book_declares_a_correlation_budget() -> None:
    """I-16: an fx_scalp and an fx_swing EURUSD long are one exposure.
    A per-book correlation budget would hide that instead of containing it."""

    assert "max_correlated_cluster_risk_pct" not in BookLimits.model_fields
    assert "max_single_instrument_risk_pct" not in BookLimits.model_fields
    assert "max_aggregate_open_risk_pct" not in BookLimits.model_fields
```

```python
# tests/unit/constitution/test_models.py additions
def test_safe_mode_triggers_are_declared_per_horizon() -> None:
    triggers = parse_constitution_yaml(CONSTITUTION_BYTES).safe_mode_triggers
    assert set(triggers) == {Horizon.SCALP, Horizon.SWING}
    assert triggers[Horizon.SCALP].max_tick_age_seconds < triggers[Horizon.SWING].max_tick_age_seconds


def test_every_declared_book_horizon_has_safe_mode_triggers() -> None:
    constitution = parse_constitution_yaml(CONSTITUTION_BYTES)
    declared = {book.horizon for book in constitution.books.values()}
    assert declared <= set(constitution.safe_mode_triggers)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/acceptance/test_risk_authority.py tests/unit/constitution -q --no-cov`

Expected: FAIL — `max_aggregate_open_risk_pct` is not a `FirmLimits` field and `safe_mode_triggers` is not a mapping.

- [ ] **Step 3: Implement the firm aggregates and per-horizon triggers**

Extend `FirmLimits` with `max_aggregate_open_risk_pct: Percentage`, `max_gross_leverage: PositiveDecimal`, and rename `max_single_symbol_risk_pct` to `max_single_instrument_risk_pct`. Change `Constitution.safe_mode_triggers` to `Mapping[Horizon, SafeModeTriggers]` and add:

```python
    @model_validator(mode="after")
    def every_book_horizon_has_triggers(self) -> Self:
        missing = {book.horizon for book in self.books.values()} - set(self.safe_mode_triggers)
        if missing:
            raise ValueError("every declared book horizon requires safe-mode triggers")
        return self
```

Update `config/risk_constitution.yaml`: add the two new firm fields, rename the symbol field, and nest `safe_mode_triggers` under `scalp:` and `swing:` keys with `max_tick_age_seconds` of `2` and `30` respectively.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/acceptance/test_risk_authority.py tests/unit/constitution -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/constitution/models.py config/risk_constitution.yaml tests
git commit -m "feat: bind risk budgets firm-wide across books"
```

---

### Task 10: Generalise Signing And Add The Venue Binding

**Files:**
- Modify: `src/trading_house/constitution/signing.py`, `src/trading_house/constitution/loader.py`
- Create: `src/trading_house/constitution/binding.py`, `config/venue_binding.mt5.yaml`
- Test: `tests/unit/constitution/test_binding.py`

**Interfaces:**
- Produces: `VerifiedArtifact`, `load_signed(artifact_path, signature_path, public_key_path) -> VerifiedArtifact`; `VenueBinding`, `load_venue_binding(...) -> VenueBinding`.
- Consumes: `BookId`, `InstrumentId` (Task 1); existing `verify_signature`, `decode_signature`, `load_public_key`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/constitution/test_binding.py
import pytest

from trading_house.constitution.binding import VenueBinding, parse_venue_binding
from trading_house.core.errors import ConfigurationError

BINDING = b"""
venue: mt5
books:
  fx_scalp: {magic_range: [110000, 119999]}
  fx_swing: {magic_range: [120000, 129999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD.raw"}
"""


def test_binding_parses() -> None:
    binding = parse_venue_binding(BINDING)
    assert binding.books["fx_scalp"].magic_range == (110000, 119999)
    assert binding.instruments["fx.eurusd"].server_symbol == "EURUSD.raw"


def test_magic_ranges_must_not_overlap() -> None:
    """Overlapping ranges make book identity unrecoverable after a restart."""

    overlapping = BINDING.replace(b"[120000, 129999]", b"[119000, 129999]")
    with pytest.raises(ConfigurationError):
        parse_venue_binding(overlapping)


def test_magic_ranges_must_be_ordered() -> None:
    with pytest.raises(ConfigurationError):
        parse_venue_binding(BINDING.replace(b"[110000, 119999]", b"[119999, 110000]"))


def test_binding_is_frozen() -> None:
    binding = parse_venue_binding(BINDING)
    with pytest.raises(Exception):
        binding.books["fx_scalp"].magic_range = (1, 2)


def test_malformed_binding_is_redacted() -> None:
    with pytest.raises(ConfigurationError):
        parse_venue_binding(b"venue: [")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/constitution/test_binding.py -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.constitution.binding'`.

- [ ] **Step 3: Generalise the verified loader**

In `src/trading_house/constitution/loader.py`, extract the signature-then-parse ordering into:

```python
@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    """Exact verified bytes plus immutable provenance."""

    content: bytes
    sha256: str
    public_key_fingerprint: str


def load_signed(
    artifact_path: Path, signature_path: Path, public_key_path: Path
) -> VerifiedArtifact:
    """Verify exact bytes before any decoding. Never parses."""
```

Move the existing body of `load_constitution` into `load_signed`, and reimplement `load_constitution` on top of it:

```python
def load_constitution(
    yaml_path: Path, signature_path: Path, public_key_path: Path
) -> LoadedConstitution:
    verified = load_signed(yaml_path, signature_path, public_key_path)
    return LoadedConstitution(
        constitution=parse_constitution_yaml(verified.content),
        constitution_sha256=verified.sha256,
        public_key_fingerprint=verified.public_key_fingerprint,
    )
```

The existing loader tests must stay green unchanged — that is the regression check on this refactor.

- [ ] **Step 4: Implement the venue binding**

```python
# src/trading_house/constitution/binding.py
"""The signed, venue-specific projection of neutral book and instrument ids."""

from itertools import combinations
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, PositiveInt, ValidationError, model_validator

from trading_house.constitution.loader import load_signed
from trading_house.constitution.models import ConstitutionModel
from trading_house.core.errors import ConfigurationError
from trading_house.core.values import NonEmptyStr
from trading_house.core.values import BookId, InstrumentId


class BookBinding(ConstitutionModel):
    magic_range: tuple[PositiveInt, PositiveInt]

    @model_validator(mode="after")
    def range_is_ordered(self) -> Self:
        if self.magic_range[0] >= self.magic_range[1]:
            raise ValueError("magic_range must be ascending")
        return self


class InstrumentBinding(ConstitutionModel):
    server_symbol: NonEmptyStr


class VenueBinding(ConstitutionModel):
    venue: Literal["mt5"]
    books: dict[BookId, BookBinding] = Field(min_length=1)
    instruments: dict[InstrumentId, InstrumentBinding] = Field(min_length=1)

    @model_validator(mode="after")
    def magic_ranges_do_not_overlap(self) -> Self:
        for (_, left), (_, right) in combinations(self.books.items(), 2):
            if left.magic_range[0] <= right.magic_range[1] and right.magic_range[0] <= left.magic_range[1]:
                raise ValueError("magic ranges must not overlap")
        return self


def parse_venue_binding(data: bytes) -> VenueBinding:
    try:
        parsed = yaml.safe_load(data)
        if not isinstance(parsed, dict):
            raise ConfigurationError()
        return VenueBinding.model_validate(parsed)
    except (yaml.YAMLError, UnicodeDecodeError, ValidationError) as error:
        raise ConfigurationError() from error


def load_venue_binding(
    binding_path: Path, signature_path: Path, public_key_path: Path
) -> VenueBinding:
    """Verify the binding signature before parsing it."""

    return parse_venue_binding(load_signed(binding_path, signature_path, public_key_path).content)
```

Create `config/venue_binding.mt5.yaml` with the four books from spec §3.3 and at least `fx.eurusd` and `metal.xauusd`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/constitution -q --no-cov`

Expected: PASS, including the untouched loader tests.

- [ ] **Step 6: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/constitution config/venue_binding.mt5.yaml tests/unit/constitution
git commit -m "feat: sign the venue binding with the constitution loader"
```

---

### Task 11: The AgentProvider Boundary

Makes I-11 executable.

**Files:**
- Create: `src/trading_house/agents/__init__.py`, `src/trading_house/agents/providers/__init__.py`, `src/trading_house/agents/providers/base.py`
- Test: `tests/unit/agents/test_providers.py`
- Modify: `tests/acceptance/test_architecture.py`

**Interfaces:**
- Produces: `Plane`, `ProviderCapabilities`, `RunBudget`, `RunOutcome`, `AgentTask`, `AgentRun`, `SandboxHandle`, `AgentProvider`.
- Consumes: `CanonicalModel`, `NonEmptyStr`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/agents/test_providers.py
import pytest
from pydantic import ValidationError

from trading_house.agents.providers.base import (
    AgentProvider,
    AgentRun,
    Plane,
    RunBudget,
    RunOutcome,
    SandboxHandle,
)


def test_shell_is_permitted_only_in_the_research_sandbox() -> None:
    """I-11: no process holding broker credentials may execute agent code."""

    assert Plane.RESEARCH_SANDBOX.shell_permitted is True
    assert Plane.CONTROL.shell_permitted is False
    assert Plane.HOT.shell_permitted is False


def test_a_sandbox_never_carries_credentials() -> None:
    handle = SandboxHandle(
        sandbox_id="s-1", plane=Plane.RESEARCH_SANDBOX,
        network_egress_allowed=False, has_broker_credentials=False,
    )
    assert handle.has_broker_credentials is False


def test_a_sandbox_with_credentials_is_unrepresentable() -> None:
    with pytest.raises(ValidationError, match="credentials"):
        SandboxHandle(
            sandbox_id="s-1", plane=Plane.RESEARCH_SANDBOX,
            network_egress_allowed=False, has_broker_credentials=True,
        )


def test_budget_ceilings_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        RunBudget(wall_clock_seconds=0, max_tokens=1, max_cost_usd_millis=1, max_tool_calls=1)


def test_a_run_records_everything_needed_to_reconstruct_it() -> None:
    required = {"provider_id", "model_id", "prompt_sha256", "transcript_sha256",
                "diff_sha256", "outcome", "tokens_used", "started_at", "finished_at"}
    assert required <= set(AgentRun.model_fields)


def test_budget_exceeded_is_a_first_class_outcome() -> None:
    assert RunOutcome.BUDGET_EXCEEDED in set(RunOutcome)


def test_provider_surface_is_two_methods() -> None:
    assert {n for n in vars(AgentProvider) if not n.startswith("_")} == {"capabilities", "run"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/agents -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.agents'`.

- [ ] **Step 3: Implement the provider boundary**

```python
# src/trading_house/agents/providers/base.py
"""Coding and reasoning models behind one interface, exactly as brokers are."""

from datetime import datetime
from enum import Enum
from typing import Protocol, Self, runtime_checkable

from pydantic import NonNegativeInt, PositiveInt, model_validator

from trading_house.core.values import CanonicalModel, NonEmptyStr


class Plane(Enum):
    """Where an agent runs decides what it may do (invariant I-11)."""

    RESEARCH_SANDBOX = ("research_sandbox", True)
    CONTROL = ("control", False)
    HOT = ("hot", False)

    def __init__(self, wire_value: str, shell_permitted: bool) -> None:
        self._value_ = wire_value
        self.shell_permitted = shell_permitted


class RunOutcome(str, Enum):  # noqa: UP042
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    REFUSED = "refused"


class ProviderCapabilities(CanonicalModel):
    can_write_code: bool
    can_run_shell: bool
    supports_tools: bool
    max_context: PositiveInt
    deterministic_seed: bool


class RunBudget(CanonicalModel):
    """Every ceiling an unattended agent loop must respect."""

    wall_clock_seconds: PositiveInt
    max_tokens: PositiveInt
    max_cost_usd_millis: PositiveInt
    max_tool_calls: PositiveInt


class SandboxHandle(CanonicalModel):
    sandbox_id: NonEmptyStr
    plane: Plane
    network_egress_allowed: bool
    has_broker_credentials: bool

    @model_validator(mode="after")
    def credentials_never_meet_agent_code(self) -> Self:
        if self.has_broker_credentials:
            raise ValueError("a sandbox may never hold broker credentials")
        return self


class AgentTask(CanonicalModel):
    task_id: NonEmptyStr
    plane: Plane
    instruction_sha256: NonEmptyStr


class AgentRun(CanonicalModel):
    """A forensic record appended to the audit ledger."""

    run_id: NonEmptyStr
    task_id: NonEmptyStr
    provider_id: NonEmptyStr
    model_id: NonEmptyStr
    prompt_sha256: NonEmptyStr
    transcript_sha256: NonEmptyStr
    diff_sha256: NonEmptyStr | None
    outcome: RunOutcome
    tokens_used: NonNegativeInt
    started_at: datetime
    finished_at: datetime


@runtime_checkable
class AgentProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def run(self, task: AgentTask, sandbox: SandboxHandle, budget: RunBudget) -> AgentRun: ...
```

- [ ] **Step 4: Add the architecture guard for I-11**

Append to `tests/acceptance/test_architecture.py`:

```python
def test_no_agent_provider_reaches_the_database_or_broker() -> None:
    """I-11: the provider boundary must not be able to see credentials."""

    tree = ast.parse(
        (SOURCE_ROOT / "agents" / "providers" / "base.py").read_text(encoding="utf-8")
    )
    forbidden = {"psycopg", "trading_house.settings", "trading_house.database"}
    assert not _imported_top_level(tree) & {"psycopg"}
    assert not {name for name in _imported_names(tree) if name in forbidden}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/agents tests/acceptance/test_architecture.py -q --no-cov`

Expected: PASS.

- [ ] **Step 6: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/agents tests/unit/agents tests/acceptance/test_architecture.py
git commit -m "feat: bound agent providers to the research plane"
```

---

### Task 12: Strategy Packages And The Trial Ledger

Makes I-12 executable.

**Files:**
- Create: `src/trading_house/research/__init__.py`, `src/trading_house/research/packages.py`, `src/trading_house/research/trial_ledger.py`
- Test: `tests/unit/research/test_packages.py`, `tests/unit/research/test_trial_ledger.py`

**Interfaces:**
- Produces: `StrategySpec`, `StrategyPackage`, `PromotionStage`; `TrialStatus`, `Trial`, `TrialLedger`, `deflation_trial_count(trials) -> int`.
- Consumes: `AssetClass`, `Horizon`, `BookId` (Task 1); `VerifiedArtifact` (Task 10).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/research/test_trial_ledger.py
import pytest
from pydantic import ValidationError

from trading_house.research.trial_ledger import Trial, TrialStatus, deflation_trial_count


def _trial(index: int, status: TrialStatus) -> Trial:
    return Trial(
        trial_id=f"t-{index}", spec_id="s-1", agent_run_id="r-1",
        status=status, sharpe=1.0, registered_at_sequence=index,
    )


def test_abandoned_trials_still_count_toward_deflation() -> None:
    """I-12: an agent loop that only logs its winners makes every statistic fiction."""

    trials = [
        _trial(1, TrialStatus.COMPLETED),
        _trial(2, TrialStatus.ABANDONED),
        _trial(3, TrialStatus.FAILED),
    ]
    assert deflation_trial_count(trials) == 3


def test_deflation_count_is_never_the_shortlist() -> None:
    trials = [_trial(i, TrialStatus.ABANDONED) for i in range(1, 51)]
    trials.append(_trial(51, TrialStatus.COMPLETED))
    completed = [t for t in trials if t.status is TrialStatus.COMPLETED]
    assert deflation_trial_count(trials) == 51
    assert deflation_trial_count(trials) != len(completed)


def test_a_completed_trial_requires_a_sharpe() -> None:
    with pytest.raises(ValidationError, match="sharpe"):
        Trial(trial_id="t-1", spec_id="s-1", agent_run_id="r-1",
              status=TrialStatus.COMPLETED, sharpe=None, registered_at_sequence=1)


def test_every_trial_names_the_agent_run_that_produced_it() -> None:
    assert "agent_run_id" in Trial.model_fields
```

```python
# tests/unit/research/test_packages.py
def test_a_package_cannot_reach_live_without_a_signature() -> None:
    assert "signature_sha256" in StrategyPackage.model_fields
    assert "trial_ledger_reference" in StrategyPackage.model_fields


def test_paper_promotion_needs_no_human_but_live_does() -> None:
    assert PromotionStage.PAPER.requires_human_signature is False
    assert PromotionStage.LIVE.requires_human_signature is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/research -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.research'`.

- [ ] **Step 3: Implement the trial ledger and packages**

```python
# src/trading_house/research/trial_ledger.py
"""Every trial the foundry ran, including the ones it did not like."""

from collections.abc import Iterable, Sequence
from enum import Enum
from typing import Protocol, Self

from pydantic import PositiveInt, model_validator

from trading_house.core.values import CanonicalModel, FiniteFloat, NonEmptyStr


class TrialStatus(str, Enum):  # noqa: UP042
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    FAILED = "failed"


class Trial(CanonicalModel):
    trial_id: NonEmptyStr
    spec_id: NonEmptyStr
    agent_run_id: NonEmptyStr
    status: TrialStatus
    sharpe: FiniteFloat | None
    registered_at_sequence: PositiveInt

    @model_validator(mode="after")
    def completed_trials_report_a_result(self) -> Self:
        if self.status is TrialStatus.COMPLETED and self.sharpe is None:
            raise ValueError("a completed trial requires a sharpe")
        return self


def deflation_trial_count(trials: Iterable[Trial]) -> int:
    """The denominator for DSR and PBO: every trial attempted, without exception."""

    return sum(1 for _ in trials)


class TrialLedger(Protocol):
    def register(self, trial: Trial) -> None: ...
    def all_trials(self, spec_id: str) -> Sequence[Trial]: ...
```

```python
# src/trading_house/research/packages.py
"""A generated strategy is data until it is signed."""

from enum import Enum

from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.core.values import AssetClass, BookId, Horizon


class PromotionStage(Enum):
    """Auto-deploy reaches paper. Only a human signature reaches capital."""

    SANDBOX = ("sandbox", False)
    PAPER = ("paper", False)
    LIVE = ("live", True)

    def __init__(self, wire_value: str, requires_human_signature: bool) -> None:
        self._value_ = wire_value
        self.requires_human_signature = requires_human_signature


class StrategySpec(CanonicalModel):
    spec_id: NonEmptyStr
    hypothesis: NonEmptyStr
    book: BookId
    horizon: Horizon
    asset_classes: tuple[AssetClass, ...]


class StrategyPackage(CanonicalModel):
    package_id: NonEmptyStr
    spec: StrategySpec
    source_sha256: NonEmptyStr
    trial_ledger_reference: NonEmptyStr
    validation_report_sha256: NonEmptyStr
    signature_sha256: NonEmptyStr | None
    stage: PromotionStage
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/research -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/research tests/unit/research
git commit -m "feat: count every trial the foundry attempts"
```

---

### Task 13: Memory Schemas And Point-In-Time Reads

Makes I-13 and I-14 executable.

**Files:**
- Create: `src/trading_house/memory/__init__.py`, `src/trading_house/memory/models.py`, `src/trading_house/memory/reader.py`
- Test: `tests/unit/memory/test_models.py`, `tests/unit/memory/test_reader.py`

**Interfaces:**
- Produces: `MemoryStore`, `ObservedFact`, `AgentBelief`, `SlippageObservation`, `FactReader` protocol, `shrink_toward_prior(observed, prior, observations, half_life)`.
- Consumes: `InstrumentId` (Task 1).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/memory/test_models.py
import pytest
from datetime import UTC, datetime
from pydantic import ValidationError

from trading_house.memory.models import AgentBelief, MemoryStore, ObservedFact, WriterKind

WHEN = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)


def test_a_fact_may_only_be_written_by_deterministic_code() -> None:
    """I-13: an agent that can write a fact can launder a belief into the hot path."""

    with pytest.raises(ValidationError, match="deterministic"):
        ObservedFact(
            fact_id="f-1", store=MemoryStore.A, written_by=WriterKind.AGENT,
            instrument_id="fx.eurusd", metric="slippage_bps", value=1.5,
            observed_at=WHEN, availability_time=WHEN,
        )


def test_a_deterministic_writer_is_accepted() -> None:
    fact = ObservedFact(
        fact_id="f-1", store=MemoryStore.A, written_by=WriterKind.DETERMINISTIC,
        instrument_id="fx.eurusd", metric="slippage_bps", value=1.5,
        observed_at=WHEN, availability_time=WHEN,
    )
    assert fact.store is MemoryStore.A


def test_a_belief_always_names_its_generating_run() -> None:
    with pytest.raises(ValidationError):
        AgentBelief(
            belief_id="b-1", store=MemoryStore.B, agent_run_id=None,
            claim="EURUSD trends after London open", availability_time=WHEN,
        )


def test_a_belief_can_never_be_stored_in_store_a() -> None:
    with pytest.raises(ValidationError, match="store"):
        AgentBelief(
            belief_id="b-1", store=MemoryStore.A, agent_run_id="r-1",
            claim="x", availability_time=WHEN,
        )
```

```python
# tests/unit/memory/test_reader.py
def test_reads_never_see_the_future() -> None:
    """I-14: a backtest that reads tomorrow's slippage is not a backtest."""

    facts = [_fact("f-1", availability=EARLY), _fact("f-2", availability=LATE)]
    visible = read_as_of(facts, as_of=MIDDLE)
    assert [fact.fact_id for fact in visible] == ["f-1"]


def test_a_fact_available_exactly_at_the_instant_is_visible() -> None:
    facts = [_fact("f-1", availability=MIDDLE)]
    assert len(read_as_of(facts, as_of=MIDDLE)) == 1


def test_shrinkage_pulls_a_thin_sample_toward_the_prior() -> None:
    assert shrink_toward_prior(observed=10.0, prior=1.0, observations=1, half_life=32) < 6.0


def test_shrinkage_trusts_a_thick_sample() -> None:
    assert shrink_toward_prior(observed=10.0, prior=1.0, observations=10_000, half_life=32) > 9.5
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/memory -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.memory'`.

- [ ] **Step 3: Implement the memory schemas**

```python
# src/trading_house/memory/models.py
"""Two stores, split by epistemic status. Facts may reach the hot path; beliefs may not."""

from datetime import datetime
from enum import Enum
from typing import Literal, Self

from pydantic import field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.values import CanonicalModel, FiniteFloat, NonEmptyStr
from trading_house.core.values import InstrumentId


class MemoryStore(str, Enum):  # noqa: UP042
    A = "observed_facts"
    B = "agent_beliefs"


class WriterKind(str, Enum):  # noqa: UP042
    DETERMINISTIC = "deterministic"
    AGENT = "agent"


class _PointInTime(CanonicalModel):
    availability_time: datetime

    @field_validator("availability_time")
    @classmethod
    def normalize(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error


class ObservedFact(_PointInTime):
    fact_id: NonEmptyStr
    store: Literal[MemoryStore.A]
    written_by: WriterKind
    instrument_id: InstrumentId
    metric: NonEmptyStr
    value: FiniteFloat
    observed_at: datetime

    @model_validator(mode="after")
    def only_deterministic_code_writes_facts(self) -> Self:
        if self.written_by is not WriterKind.DETERMINISTIC:
            raise ValueError("only deterministic post-trade code may write a fact")
        return self


class AgentBelief(_PointInTime):
    belief_id: NonEmptyStr
    store: Literal[MemoryStore.B]
    agent_run_id: NonEmptyStr
    claim: NonEmptyStr
```

```python
# src/trading_house/memory/reader.py
"""Point-in-time reads. A memory read that sees the future is a leak."""

from collections.abc import Sequence
from datetime import datetime

from trading_house.memory.models import ObservedFact


def read_as_of(facts: Sequence[ObservedFact], *, as_of: datetime) -> tuple[ObservedFact, ...]:
    """Return only facts that were available at the given instant (I-14)."""

    return tuple(fact for fact in facts if fact.availability_time <= as_of)


def shrink_toward_prior(
    *, observed: float, prior: float, observations: int, half_life: int
) -> float:
    """Blend an empirical estimate toward a prior on an explicit half-life.

    Memory that chases last week's regime is worse than no memory.
    """

    if observations < 0 or half_life <= 0:
        raise ValueError("observations must be non-negative and half_life positive")
    weight = observations / (observations + half_life)
    return prior + weight * (observed - prior)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/memory -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/memory tests/unit/memory
git commit -m "feat: separate observed facts from agent beliefs"
```

---

### Task 14: Migration 0002 — Memory And Trial Tables

**Files:**
- Create: `migrations/versions/0002_memory_and_trials.py`
- Test: `tests/integration/database/test_memory_migration.py`

**Interfaces:**
- Produces: `memory.observed_facts`, `memory.agent_beliefs`, `research.trials`, and the role grants enforcing I-13.
- Consumes: the role names created by migration `0001`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/database/test_memory_migration.py
import psycopg
import pytest

pytestmark = pytest.mark.integration


def test_memory_and_research_schemas_exist(database) -> None:
    with psycopg.connect(database.runtime_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('memory.observed_facts'), "
            "to_regclass('memory.agent_beliefs'), to_regclass('research.trials')"
        )
        assert cursor.fetchone() == (
            "memory.observed_facts", "memory.agent_beliefs", "research.trials",
        )


def test_runtime_cannot_insert_a_fact_directly(database) -> None:
    """I-13 at the database boundary, not just in the model."""

    with (
        psycopg.connect(database.runtime_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cursor.execute("INSERT INTO memory.observed_facts (fact_id) VALUES ('f-1')")


def test_facts_are_append_only(database) -> None:
    with (
        psycopg.connect(database.migration_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.RaiseException),
    ):
        cursor.execute("SET ROLE trading_house_owner")
        cursor.execute("UPDATE memory.observed_facts SET metric = 'x'")


def test_trials_are_append_only(database) -> None:
    with (
        psycopg.connect(database.migration_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.RaiseException),
    ):
        cursor.execute("SET ROLE trading_house_owner")
        cursor.execute("DELETE FROM research.trials")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/database/test_memory_migration.py -m integration -q --no-cov`

Expected: FAIL — `to_regclass` returns `None` for all three tables.

- [ ] **Step 3: Write the migration**

Create `migrations/versions/0002_memory_and_trials.py` with `down_revision = "0001_audit_ledger"`. Follow the exact pattern of `0001_audit_ledger.py`:

- `SET ROLE trading_house_owner`, then `CREATE SCHEMA memory` and `CREATE SCHEMA research`, both `AUTHORIZATION trading_house_owner`, then `REVOKE ALL ... FROM PUBLIC`.
- `memory.observed_facts`, exactly:

```sql
CREATE TABLE memory.observed_facts (
    fact_id TEXT PRIMARY KEY,
    written_by TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    availability_time TIMESTAMPTZ NOT NULL,
    previous_hash BYTEA NOT NULL,
    entry_hash BYTEA NOT NULL,
    CONSTRAINT facts_written_by_deterministic CHECK (written_by = 'deterministic'),
    CONSTRAINT facts_previous_hash_size CHECK (
        pg_catalog.octet_length(previous_hash) = 32
    ),
    CONSTRAINT facts_entry_hash_size CHECK (
        pg_catalog.octet_length(entry_hash) = 32
    ),
    CONSTRAINT facts_availability_not_before_observation CHECK (
        availability_time >= observed_at
    )
)
```

  The `facts_written_by_deterministic` CHECK is I-13 expressed in the schema
  itself, so it holds even against a direct connection.
- `memory.agent_beliefs`: `belief_id TEXT PRIMARY KEY`, `agent_run_id TEXT NOT NULL`, `claim TEXT NOT NULL`, `availability_time TIMESTAMPTZ NOT NULL`.
- `research.trials`: `trial_id TEXT PRIMARY KEY`, `spec_id TEXT NOT NULL`, `agent_run_id TEXT NOT NULL`, `status TEXT NOT NULL`, `sharpe DOUBLE PRECISION`, `registered_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp()`.
- Reuse the `audit.reject_ledger_row_mutation()` pattern: create one `memory.reject_row_mutation()` trigger function with `SET search_path = pg_catalog`, and attach `BEFORE UPDATE OR DELETE` triggers to all three tables.
- Grants: `GRANT USAGE ON SCHEMA memory, research TO trading_house_runtime`; `GRANT SELECT` on all three tables to the runtime role; **no `INSERT` on `memory.observed_facts`** for the runtime role — that is I-13 at the database boundary. `GRANT INSERT ON memory.agent_beliefs, research.trials TO trading_house_runtime`.
- Write a `downgrade()` that reverses every statement in order.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/database -m integration -q --no-cov`

Expected: PASS. The `isolated_audit_ledger` fixture downgrades to base and back, so `0002` must round-trip cleanly.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/0002_memory_and_trials.py tests/integration/database/test_memory_migration.py
git commit -m "feat: create append-only memory and trial tables"
```

---

### Task 15: Re-sign, Rewire The CLI, And Accept The Phase

**Files:**
- Modify: `src/trading_house/cli.py`, `README.md`
- Create: `config/risk_constitution.yaml.sig` (regenerated), `config/venue_binding.mt5.yaml.sig`
- Modify: `tests/acceptance/test_phase0.py`
- Create: `tests/acceptance/test_phase0_5.py`

**Interfaces:**
- Consumes: everything from Tasks 1–14.
- Produces: a green acceptance suite and a `binding verify` command.

- [ ] **Step 1: Write the failing acceptance test**

```python
# tests/acceptance/test_phase0_5.py
"""Phase 0.5 acceptance: the revised contracts hold end to end."""

import ast
from pathlib import Path

from trading_house.constitution.binding import load_venue_binding
from trading_house.constitution.loader import load_constitution

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT_ROOT / "config"


def test_the_revised_constitution_verifies() -> None:
    loaded = load_constitution(
        CONFIG / "risk_constitution.yaml",
        CONFIG / "risk_constitution.yaml.sig",
        CONFIG / "risk_constitution.public.pem",
    )
    assert set(loaded.constitution.books) == {"fx_scalp", "fx_swing", "equity_swing", "sleeve"}


def test_the_venue_binding_verifies_and_covers_every_book() -> None:
    binding = load_venue_binding(
        CONFIG / "venue_binding.mt5.yaml",
        CONFIG / "venue_binding.mt5.yaml.sig",
        CONFIG / "risk_constitution.public.pem",
    )
    loaded = load_constitution(
        CONFIG / "risk_constitution.yaml",
        CONFIG / "risk_constitution.yaml.sig",
        CONFIG / "risk_constitution.public.pem",
    )
    assert set(binding.books) == set(loaded.constitution.books)


def test_the_constitution_names_no_venue() -> None:
    """A signed constitution must survive changing brokers."""

    text = (CONFIG / "risk_constitution.yaml").read_text(encoding="utf-8").lower()
    for token in ("mt5", "magic", "metatrader", "server_symbol"):
        assert token not in text


def test_no_canonical_module_mentions_a_broker_encoding() -> None:
    forbidden = {"magic", "deviation_points", "retcode", "filling"}
    for name in ("values.py", "instruments.py", "schemas.py"):
        tree = ast.parse((PROJECT_ROOT / "src" / "trading_house" / "core" / name).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        assert forbidden.isdisjoint(names), name
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/acceptance/test_phase0_5.py -q --no-cov`

Expected: FAIL — the committed signature no longer matches the rewritten YAML (Task 8 changed it).

- [ ] **Step 3: Regenerate both signatures**

```bash
uv run trading-house constitution sign --constitution config/risk_constitution.yaml --private-key .local/keys/risk_constitution.private.pem --signature-output config/risk_constitution.yaml.sig --force
```

```bash
uv run trading-house constitution sign --constitution config/venue_binding.mt5.yaml --private-key .local/keys/risk_constitution.private.pem --signature-output config/venue_binding.mt5.yaml.sig
```

- [ ] **Step 4: Add the binding verify command**

In `src/trading_house/cli.py`, add to the `constitution` group:

```python
@constitution_app.command("binding")
def constitution_binding(
    binding: Annotated[Path, typer.Option("--binding")] = Path("config/venue_binding.mt5.yaml"),
    signature: Annotated[Path, typer.Option("--signature")] = Path(
        "config/venue_binding.mt5.yaml.sig"
    ),
    public_key: Annotated[Path, typer.Option("--public-key")] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Verify the signed venue binding and report its coverage."""

    def operation() -> dict[str, JsonValue]:
        loaded = load_venue_binding(binding, signature, public_key)
        return {
            "venue": loaded.venue,
            "books": sorted(loaded.books),
            "instruments": sorted(loaded.instruments),
        }

    _run(operation)
```

Add `from trading_house.constitution.binding import load_venue_binding` to the imports.

- [ ] **Step 5: Update the Phase 0 acceptance test and README**

In `tests/acceptance/test_phase0.py`, update `test_checked_in_verification_artifacts_exist` to also assert `config/venue_binding.mt5.yaml` and its signature exist.

In `README.md`, add the `constitution binding` row to the operator command table, document the two signed artifacts under private-key handling, and add a short "Books" section listing the four declared books and the fact that they share one MT5 account's margin.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest`

Expected: PASS with coverage at or above 95%.

- [ ] **Step 7: Run the full verification gate**

Run: `uv lock --check`

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy`

Run: `uv run trading-house constitution verify`

Run: `uv run trading-house constitution binding`

Run: `git status --short`

Expected: every command exits `0`; git status shows no private key, `.env`, or generated artifact.

- [ ] **Step 8: Commit**

```bash
git add config src/trading_house/cli.py README.md tests/acceptance
git commit -m "docs: accept the phase 0.5 contract revision"
```

---

## Spec Sections Deliberately Not In This Plan

These are in the design spec but belong to later phases per its own roadmap
(§5.2). They are listed so an implementer can see the omission is intentional.

| Spec section | Where it lands | Why not now |
|---|---|---|
| §3.5 Circuit-breaker scoping | Phase 2 (risk core) | The book/house/safe-mode scoping table is engine behaviour. Its *data* — per-horizon `safe_mode_triggers` — does land here, in Task 9 |
| §4.1 Gateway priority queue | Phase 1 | Needs a real gateway actor to queue anything |
| §4.2 Hot-loop data flow | Phases 1–3 | Needs features, strategies and a position guard |
| §4.3 Idempotency spine | Phase 3 (execution) | `IntentState` and deterministic magic derivation land here; the state machine and crash recovery need a live adapter |
| §4.5 Fault-injection harness | Phase 1 | `BrokerAdapter` lands here (Task 7), which is precisely what makes a `FaultyAdapter` writable later |
| §2.3 Foundry orchestration loop | Phase 5.5 | Requires the promotion gates and backtester from Phase 5 |
| §2.4 Learned cost model | Phase 3.5 | Requires realised fills, which require execution |

## Final Verification Gate

After Task 15, run these from the repository root without relying on earlier output:

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run trading-house constitution verify
uv run trading-house constitution binding
git status --short
```

Expected results:

- every command exits `0`;
- pytest reports no failures and coverage at or above 95%;
- `constitution verify` reports version `1` and four declared books;
- `constitution binding` reports `mt5` with a magic range per book;
- git status contains no private key, `.env`, database volume, or generated artifact;
- `tests/acceptance/test_architecture.py` and `tests/acceptance/test_phase0.py` are still green — Phase 0's guarantees survived the revision.

## Handoff To Phase 1

Phase 1 may add the MT5 gateway only after this gate is green. It must:

- implement `BrokerAdapter` rather than defining its own surface;
- keep every MT5 encoding — magic, deviation points, fill modes, retcodes — inside `brokers/mt5/`, never in `core/`;
- derive `magic` deterministically from `intent_id` within the book's range from the signed venue binding;
- add the fifth health-gate step, reconciling every book against the venue before reporting ready;
- branch recovery on `recovery_for()`, never widening a stop.

