# Phase 0 Safety Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a non-trading Python foundation with strict canonical contracts, UTC discipline, Ed25519-verified risk configuration, and a PostgreSQL append-only audit chain that must verify before readiness.

**Architecture:** Use a Python 3.12 modular monolith with narrow packages for core contracts, constitution authority, PostgreSQL access, audit integrity, and operator health commands. PostgreSQL 18 owns serialized audit appends through a locked security-definer function; application code independently recomputes the chain. No broker, strategy, risk-engine, execution, or LLM path exists in this phase.

**Tech Stack:** Python 3.12, uv, Pydantic v2, pydantic-settings, cryptography/Ed25519, PyYAML, RFC 8785, Psycopg 3, Alembic, PostgreSQL 18/pgcrypto, Typer, pytest, Hypothesis, Ruff, mypy, Docker Compose, GitHub Actions.

## Global Constraints

- Implement Phase 0 only; do not add `MetaTrader5`, LangGraph, an LLM SDK, broker code, strategies, sizing, or order execution.
- Preserve invariants I-1 through I-10 from `mt5-multi-agent-trading-house-spec.md`; Phase 0 directly establishes I-2, I-9's future ledger boundary, and I-10's timestamp contracts.
- Python is exactly the 3.12 release line: `>=3.12,<3.13`.
- PostgreSQL integration targets PostgreSQL 18.
- Verify the Ed25519 signature over the exact YAML bytes before UTF-8 decoding or YAML parsing.
- Runtime has only the public key. Private signing keys are external and gitignored.
- Risk-constitution models are frozen; `signature_required` must be `true`; core and sleeve capital fractions must sum exactly to `1`.
- Every canonical timestamp is timezone-aware UTC, with `event_time <= availability_time <= processing_time`.
- Runtime cannot directly insert, update, delete, truncate, or alter the audit ledger.
- Audit appends are serialized and transactional; sequence numbers may not gap after a failed transaction.
- Runtime checks the exact Alembic head but never applies migrations automatically.
- Security failures fail closed and operator messages never expose DSNs, passwords, or key material.
- Use TDD for every behavior: write the focused test, observe the expected failure, implement minimally, then run the focused and full relevant suites.
- Use `uv.lock` in CI and enforce at least 95% total statement coverage.

---

### Task 1: Reproducible Python Project and Quality Baseline

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitattributes`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/trading_house/__init__.py`
- Create: `src/trading_house/py.typed`
- Create: `tests/unit/test_package.py`
- Track unchanged: `mt5-multi-agent-trading-house-spec.md`
- Track unchanged: `deep-research-report (1).md`

**Interfaces:**
- Produces: installable package `trading_house` with `__version__ == "0.1.0"`.
- Produces: locked runtime/dev dependency graph and common pytest/Ruff/mypy settings.

- [ ] **Step 1: Add project configuration before behavioral code**

Create `pyproject.toml` with these constraints and tool settings:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "trading-house"
version = "0.1.0"
description = "Safety-first foundation for an MT5 multi-agent trading house"
readme = "README.md"
requires-python = ">=3.12,<3.13"
dependencies = [
  "alembic>=1.16,<2",
  "cryptography>=45,<52",
  "psycopg[binary]>=3.2,<4",
  "pydantic>=2.11,<3",
  "pydantic-settings>=2.9,<3",
  "pyyaml>=6,<7",
  "rfc8785>=0.1,<1",
  "typer>=0.16,<1",
]

[dependency-groups]
dev = [
  "hypothesis>=6,<7",
  "mypy>=1.16,<2",
  "pytest>=8,<10",
  "pytest-cov>=6,<8",
  "ruff>=0.12,<1",
  "testcontainers[postgres]>=4.10,<5",
  "types-pyyaml>=6,<7",
]

[tool.hatch.build.targets.wheel]
packages = ["src/trading_house"]

[tool.pytest.ini_options]
addopts = "-ra --strict-config --strict-markers --cov=trading_house --cov-report=term-missing --cov-fail-under=95"
testpaths = ["tests"]
markers = ["integration: requires PostgreSQL"]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "RUF", "S", "PT"]

[tool.mypy]
python_version = "3.12"
strict = true
plugins = ["pydantic.mypy"]
packages = ["trading_house"]
```

Set `.python-version` to `3.12`, force LF for `*.py`, `*.sh`, `*.yaml`, `*.yml`, and `*.md`, and ignore `.venv/`, `.env`, `.local/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, coverage artifacts, and Python bytecode.

Create `README.md` with the project name, a link to the Phase 0 design, and the
explicit statement: "Phase 0 contains no broker connection or trading path."

- [ ] **Step 2: Resolve and lock dependencies**

Run: `uv lock && uv sync --locked --all-groups`

Expected: exit `0`; `uv.lock` is created and Python 3.12 is selected.

- [ ] **Step 3: Write package and console-entry lifecycle contract tests**

```python
import importlib.util
import tomllib
from pathlib import Path


def test_package_exposes_version() -> None:
    import trading_house

    assert trading_house.__version__ == "0.1.0"


def test_console_entry_point_matches_cli_module_availability() -> None:
    project_root = Path(__file__).parents[2]
    metadata = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = metadata["project"].get("scripts", {})

    try:
        cli_available = importlib.util.find_spec("trading_house.cli") is not None
    except ModuleNotFoundError:
        cli_available = False

    if not cli_available:
        assert "trading-house" not in scripts
    else:
        assert scripts.get("trading-house") == "trading_house.cli:app"
```

The lifecycle test must remain valid after Task 12: it rejects a premature
entry point while the CLI module is absent and requires the exact
`trading_house.cli:app` mapping once the module exists. Task 12 owns adding that
mapping.

- [ ] **Step 4: Run the test and verify the missing contract**

Run: `uv run pytest tests/unit/test_package.py -q --no-cov`

Expected: FAIL because `trading_house.__version__` is absent. The console-entry
lifecycle test passes because neither the CLI module nor its mapping exists.

- [ ] **Step 5: Add the minimal package implementation**

```python
"""Safety-first trading-house foundation."""

__version__ = "0.1.0"
```

- [ ] **Step 6: Verify package and static baseline**

Run: `uv run pytest tests/unit/test_package.py -q --no-cov`

Expected: PASS.

Run: `uv run ruff check . && uv run mypy src`

Expected: both exit `0`.

- [ ] **Step 7: Commit the reproducible baseline**

```powershell
git add pyproject.toml uv.lock .python-version .gitattributes .gitignore README.md src tests mt5-multi-agent-trading-house-spec.md 'deep-research-report (1).md'
git commit -m "build: establish phase 0 Python foundation"
```

---

### Task 2: Typed Errors and UTC Clock Discipline

**Files:**
- Create: `src/trading_house/core/__init__.py`
- Create: `src/trading_house/core/errors.py`
- Create: `src/trading_house/core/clock.py`
- Create: `tests/unit/core/test_clock.py`
- Create: `tests/unit/core/test_errors.py`

**Interfaces:**
- Produces: `ensure_utc(value: datetime) -> datetime`.
- Produces: `Clock.now()`, `SystemClock`, and `FixedClock`.
- Produces: typed errors and stable `ExitCode` integer values.

- [ ] **Step 1: Write failing clock and error tests**

```python
from datetime import datetime, timedelta, timezone

import pytest

from trading_house.core.clock import FixedClock, SystemClock, ensure_utc
from trading_house.core.errors import ExitCode, TimestampError


def test_ensure_utc_rejects_naive_datetime() -> None:
    with pytest.raises(TimestampError, match="timezone-aware"):
        ensure_utc(datetime(2026, 8, 3, 12, 0))


def test_ensure_utc_normalizes_an_aware_offset() -> None:
    value = datetime(2026, 8, 3, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert ensure_utc(value) == datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def test_fixed_clock_is_deterministic() -> None:
    instant = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    assert FixedClock(instant).now() == instant


def test_system_clock_returns_utc() -> None:
    assert SystemClock().now().tzinfo is timezone.utc


def test_exit_codes_are_stable() -> None:
    assert ExitCode.CONFIGURATION == 2
    assert ExitCode.SIGNATURE == 3
    assert ExitCode.DATABASE == 4
    assert ExitCode.MIGRATION == 5
    assert ExitCode.AUDIT_INTEGRITY == 6
```

- [ ] **Step 2: Run tests and observe missing modules**

Run: `uv run pytest tests/unit/core/test_clock.py tests/unit/core/test_errors.py -q --no-cov`

Expected: collection ERROR because `trading_house.core.clock` and `.errors` do not exist.

- [ ] **Step 3: Implement the error hierarchy and clocks**

Implement `TradingHouseError`, `TimestampError`, `ConfigurationError`,
`SignatureVerificationError`, `SchemaValidationError`, `DatabaseUnavailableError`,
`MigrationMismatchError`, `AuditAppendError`, and `AuditIntegrityError`.

```python
class ExitCode(IntEnum):
    OK = 0
    CONFIGURATION = 2
    SIGNATURE = 3
    DATABASE = 4
    MIGRATION = 5
    AUDIT_INTEGRITY = 6
    AUDIT_APPEND = 7


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimestampError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class FixedClock:
    instant: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "instant", ensure_utc(self.instant))

    def now(self) -> datetime:
        return self.instant
```

- [ ] **Step 4: Verify the focused and package suites**

Run: `uv run pytest tests/unit/core tests/unit/test_package.py -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/trading_house/core tests/unit/core
git commit -m "feat: enforce UTC clock discipline"
```

---

### Task 3: Stamped Market, Regime, Proposal, and Opinion Contracts

**Files:**
- Create: `src/trading_house/core/schemas.py`
- Create: `tests/unit/core/test_schemas_market.py`

**Interfaces:**
- Consumes: `ensure_utc(datetime)` from Task 2.
- Produces: `Book`, `Side`, `CanonicalModel`, `Stamped`, `RegimeAssessment`, `TradeProposal`, and `AgentOpinion`.

- [ ] **Step 1: Write failing contract tests**

Create UTC timestamp helpers and tests proving:

```python
def test_stamped_rejects_out_of_order_availability(stamp: dict[str, object]) -> None:
    stamp["availability_time"] = stamp["event_time"] - timedelta(microseconds=1)
    with pytest.raises(ValidationError, match="event_time"):
        Stamped(**stamp)


def test_trade_proposal_forbids_volume(valid_proposal: dict[str, object]) -> None:
    valid_proposal["volume"] = 5.0
    with pytest.raises(ValidationError, match="Extra inputs"):
        TradeProposal(**valid_proposal)


@pytest.mark.parametrize("probability", [0.0, 1.0])
def test_trade_proposal_rejects_degenerate_probability(
    valid_proposal: dict[str, object], probability: float
) -> None:
    valid_proposal["win_probability"] = probability
    with pytest.raises(ValidationError, match="degenerate"):
        TradeProposal(**valid_proposal)


def test_buy_proposal_requires_invalidation_below_entry(
    valid_proposal: dict[str, object],
) -> None:
    valid_proposal["invalidation_price"] = valid_proposal["entry_price_ref"]
    with pytest.raises(ValidationError, match="BUY invalidation"):
        TradeProposal(**valid_proposal)


def test_regime_probabilities_sum_to_one(stamp: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="sum to 1"):
        RegimeAssessment(
            **stamp,
            symbol="EURUSD",
            volatility_state="normal",
            trend_state="range",
            liquidity_state="deep",
            probabilities={"up": 0.8, "down": 0.8},
            uncertainty=0.2,
        )
```

Also assert models are frozen, reject unknown fields, normalize offset-aware
timestamps, require non-empty identifiers, and serialize datetimes with `Z`/UTC.

- [ ] **Step 2: Run tests and verify missing schemas**

Run: `uv run pytest tests/unit/core/test_schemas_market.py -q --no-cov`

Expected: collection ERROR because `core.schemas` does not exist.

- [ ] **Step 3: Implement the strict frozen schema base and contracts**

Use `ConfigDict(strict=True, frozen=True, extra="forbid")`, constrained annotated
types, field validators that call `ensure_utc`, and model validators for timestamp
ordering, probability sums, and side-relative invalidation.

Implement every field exactly as specified in section 4 for:

```python
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
PositiveFiniteFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegativeFiniteFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Book(str, Enum):
    CORE = "core"
    SLEEVE = "sleeve"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Stamped(CanonicalModel):
    event_time: datetime
    availability_time: datetime
    processing_time: datetime
    source: NonEmptyStr
    revision_id: NonEmptyStr | None = None
    quality_flags: tuple[NonEmptyStr, ...] = ()


class RegimeAssessment(Stamped):
    symbol: NonEmptyStr
    volatility_state: Literal["low", "normal", "high", "extreme"]
    trend_state: Literal["down", "range", "up"]
    liquidity_state: Literal["thin", "normal", "deep"]
    probabilities: dict[NonEmptyStr, Probability]
    uncertainty: Probability


class TradeProposal(Stamped):
    proposal_id: NonEmptyStr
    strategy_id: NonEmptyStr
    strategy_version: NonEmptyStr
    book: Book
    symbol: NonEmptyStr
    side: Side
    horizon_seconds: PositiveInt
    entry_condition: NonEmptyStr
    entry_price_ref: PositiveFiniteFloat
    invalidation_price: PositiveFiniteFloat
    max_holding_seconds: PositiveInt
    expected_return_bps: FiniteFloat
    expected_return_stdev_bps: NonNegativeFiniteFloat
    expected_cost_bps: NonNegativeFiniteFloat
    win_probability: Probability
    calibration_id: NonEmptyStr
    required_liquidity_lots: PositiveFiniteFloat
    regime_ref: NonEmptyStr
    features_snapshot_id: NonEmptyStr
    rationale: NonEmptyStr | None = None


class AgentOpinion(Stamped):
    agent_role: NonEmptyStr
    subject_id: NonEmptyStr
    stance: Literal["FOR", "AGAINST", "ABSTAIN"]
    evidence_for: tuple[NonEmptyStr, ...] = ()
    evidence_against: tuple[NonEmptyStr, ...] = ()
    missing_information: tuple[NonEmptyStr, ...] = ()
    confidence: Probability
```

Use `math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-9)` and reject
non-finite numeric inputs.

- [ ] **Step 4: Verify schemas and type checks**

Run: `uv run pytest tests/unit/core/test_schemas_market.py -q --no-cov`

Expected: PASS.

Run: `uv run mypy src/trading_house/core && uv run ruff check src/trading_house/core tests/unit/core`

Expected: both exit `0`.

- [ ] **Step 5: Commit**

```powershell
git add src/trading_house/core/schemas.py tests/unit/core/test_schemas_market.py
git commit -m "feat: add canonical proposal schemas"
```

---

### Task 4: Risk, Order, and Position Contracts

**Files:**
- Modify: `src/trading_house/core/schemas.py`
- Create: `tests/unit/core/test_schemas_execution.py`

**Interfaces:**
- Consumes: `Book`, `Side`, and `CanonicalModel` from Task 3.
- Produces: discriminated `RiskDecision`, `OrderIntent`, `PositionState`, and `RISK_DECISION_ADAPTER`.

- [ ] **Step 1: Write failing execution-contract tests**

```python
def test_rejected_decision_cannot_carry_executable_volume() -> None:
    with pytest.raises(ValidationError):
        RISK_DECISION_ADAPTER.validate_python(
            {
                "proposal_id": "p-1",
                "verdict": "REJECTED",
                "approved_volume_lots": 1.0,
                "stop_loss_price": 1.08,
                "take_profit_price": None,
                "risk_money": 10.0,
                "risk_pct_of_book": 0.35,
                "reasons": ["daily_loss_halt"],
                "checks_passed": [],
                "constitution_version": 1,
            }
        )


def test_order_intent_requires_protective_stop(valid_intent: dict[str, object]) -> None:
    valid_intent.pop("sl")
    with pytest.raises(ValidationError, match="sl"):
        OrderIntent(**valid_intent)


def test_order_intent_rejects_zero_stop(valid_intent: dict[str, object]) -> None:
    valid_intent["sl"] = 0.0
    with pytest.raises(ValidationError, match="greater than 0"):
        OrderIntent(**valid_intent)


def test_position_state_requires_positive_initial_risk(valid_position: dict[str, object]) -> None:
    valid_position["initial_risk_distance"] = 0.0
    with pytest.raises(ValidationError, match="greater than 0"):
        PositionState(**valid_position)
```

Also test valid approved, resized, and rejected decisions; UTC `t_submit_utc` and
`opened_at_utc`; frozen positions; and literal state/lifecycle rejection.

- [ ] **Step 2: Run tests and observe missing contracts**

Run: `uv run pytest tests/unit/core/test_schemas_execution.py -q --no-cov`

Expected: collection ERROR for missing execution contracts.

- [ ] **Step 3: Implement discriminated decisions and execution models**

```python
class BaseRiskDecision(CanonicalModel):
    proposal_id: NonEmptyStr
    reasons: tuple[NonEmptyStr, ...]
    checks_passed: tuple[NonEmptyStr, ...]
    constitution_version: PositiveInt


class ExecutableRiskDecision(BaseRiskDecision):
    approved_volume_lots: PositiveFiniteFloat
    stop_loss_price: PositiveFiniteFloat
    take_profit_price: PositiveFiniteFloat | None
    risk_money: PositiveFiniteFloat
    risk_pct_of_book: PositiveFiniteFloat


class ApprovedRiskDecision(ExecutableRiskDecision):
    verdict: Literal["APPROVED"]


class ResizedRiskDecision(ExecutableRiskDecision):
    verdict: Literal["RESIZED"]


class RejectedRiskDecision(BaseRiskDecision):
    verdict: Literal["REJECTED"]
    approved_volume_lots: Literal[0.0]
    stop_loss_price: None = None
    take_profit_price: None = None
    risk_money: Literal[0.0]
    risk_pct_of_book: Literal[0.0]


RiskDecision = Annotated[
    ApprovedRiskDecision | ResizedRiskDecision | RejectedRiskDecision,
    Field(discriminator="verdict"),
]
RISK_DECISION_ADAPTER = TypeAdapter(RiskDecision)


class OrderIntent(CanonicalModel):
    intent_id: NonEmptyStr
    proposal_id: NonEmptyStr
    magic: PositiveInt
    symbol: NonEmptyStr
    side: Side
    volume: PositiveFiniteFloat
    sl: PositiveFiniteFloat
    tp: PositiveFiniteFloat | None
    deviation_points: NonNegativeInt
    filling: NonNegativeInt
    state: Literal["SUBMITTING", "CONFIRMED", "UNKNOWN", "RECONCILING", "FAILED", "REJECTED"]
    t_submit_utc: datetime
    broker_order_ticket: PositiveInt | None = None
    broker_position_ticket: PositiveInt | None = None
    fill_price: PositiveFiniteFloat | None = None
    retcode: int | None = None


class PositionState(CanonicalModel):
    position_ticket: PositiveInt
    intent_id: NonEmptyStr | None
    strategy_id: NonEmptyStr
    book: Book
    symbol: NonEmptyStr
    side: Side
    volume: PositiveFiniteFloat
    open_price: PositiveFiniteFloat
    current_sl: PositiveFiniteFloat
    current_tp: PositiveFiniteFloat | None
    opened_at_utc: datetime
    lifecycle: Literal["OPEN_PROTECTED", "BREAKEVEN_ELIGIBLE", "TRAILING", "EXIT_PENDING", "CLOSED"]
    r_multiple_open: FiniteFloat
    mae_r: FiniteFloat
    mfe_r: FiniteFloat
    initial_risk_distance: PositiveFiniteFloat
```

Implement the remaining `OrderIntent` and `PositionState` fields and literals
exactly from section 4. Require rejection reasons to be non-empty and require a
positive SL on every order intent.

- [ ] **Step 4: Verify all canonical schemas**

Run: `uv run pytest tests/unit/core/test_schemas_market.py tests/unit/core/test_schemas_execution.py -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/trading_house/core/schemas.py tests/unit/core/test_schemas_execution.py
git commit -m "feat: make unsafe execution states unrepresentable"
```

---

### Task 5: Frozen Risk-Constitution Models and Checked-In YAML

**Files:**
- Create: `src/trading_house/constitution/__init__.py`
- Create: `src/trading_house/constitution/models.py`
- Create: `config/risk_constitution.yaml`
- Create: `tests/unit/constitution/test_models.py`

**Interfaces:**
- Produces: `Constitution`, `BookLimits`, `FirmLimits`, `Prohibitions`, and `SafeModeTriggers`.
- Produces: `parse_constitution_yaml(data: bytes) -> Constitution` using Decimal-aware safe YAML parsing.

- [ ] **Step 1: Write failing constitution tests**

Load the checked-in YAML and assert the exact section 1.4 values. Add focused
tests for missing books, `signature_required: false`, a capital sum other than
`1`, wrong prohibition literals, unknown fields, negative limits, and model
immutability.

```python
def test_checked_in_constitution_matches_spec() -> None:
    model = parse_constitution_yaml(Path("config/risk_constitution.yaml").read_bytes())
    assert model.version == 1
    assert model.books.core.risk_per_trade_pct == Decimal("0.35")
    assert model.books.sleeve.risk_per_trade_pct == Decimal("1.5")
    assert model.firm.max_total_drawdown_halt_pct == Decimal("10.0")


def test_capital_fractions_must_sum_exactly_to_one(valid_data: dict[str, object]) -> None:
    valid_data["books"]["sleeve"]["capital_fraction"] = Decimal("0.11")
    with pytest.raises(ValidationError, match="sum exactly to 1"):
        Constitution.model_validate(valid_data)
```

- [ ] **Step 2: Run tests and verify missing models/config**

Run: `uv run pytest tests/unit/constitution/test_models.py -q --no-cov`

Expected: collection ERROR because the constitution package does not exist.

- [ ] **Step 3: Implement exact frozen constitution models**

Define strict frozen Pydantic models with `Decimal` percentage/fraction fields,
positive integers for counts, positive decimals for leverage and thresholds,
`Literal[True]` for `signature_required`, and exact prohibition literals:

```python
PositiveDecimal = Annotated[Decimal, Field(gt=0)]


class BookLimits(ConstitutionModel):
    capital_fraction: PositiveDecimal
    risk_per_trade_pct: PositiveDecimal
    max_concurrent_positions: PositiveInt
    daily_loss_stop_pct: PositiveDecimal
    max_drawdown_halt_pct: PositiveDecimal
    max_gross_leverage: PositiveDecimal


class Books(ConstitutionModel):
    core: BookLimits
    sleeve: BookLimits


class FirmLimits(ConstitutionModel):
    max_total_drawdown_halt_pct: PositiveDecimal
    max_correlated_cluster_risk_pct: PositiveDecimal
    max_single_symbol_risk_pct: PositiveDecimal
    max_orders_per_minute: PositiveInt
    max_consecutive_rejects: PositiveInt


class Prohibitions(ConstitutionModel):
    martingale_sizing: Literal["forbidden"]
    averaging_into_losers: Literal["forbidden_unless_declared_in_strategy_spec"]
    stop_removal: Literal["forbidden"]
    stop_widening: Literal["forbidden"]
    leverage_increase_after_loss: Literal["forbidden"]
    trading_without_protective_stop: Literal["forbidden"]


class SafeModeTriggers(ConstitutionModel):
    max_tick_age_seconds: PositiveDecimal
    max_spread_multiple_of_median: PositiveDecimal
    max_clock_drift_ms: PositiveInt
    reconciliation_mismatch: Literal[True]
    slippage_breach_sigma: PositiveDecimal


class Constitution(ConstitutionModel):
    version: PositiveInt
    signature_required: Literal[True]
    books: Books
    firm: FirmLimits
    prohibitions: Prohibitions
    safe_mode_triggers: SafeModeTriggers
```

Use a `yaml.SafeLoader` subclass whose YAML float constructor returns
`Decimal(token)`, call `yaml.load(..., Loader=DecimalSafeLoader)`, require a
mapping root, and translate YAML/validation errors to `ConfigurationError` with
no raw secret values.

- [ ] **Step 4: Add the exact section 1.4 YAML and verify tests**

Copy the full risk constitution from the source specification without adding
security-critical defaults. Run:

`uv run pytest tests/unit/constitution/test_models.py -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/trading_house/constitution config/risk_constitution.yaml tests/unit/constitution
git commit -m "feat: model immutable risk constitution"
```

---

### Task 6: Ed25519 Signing, Verification, and Verified Loader

**Files:**
- Create: `src/trading_house/constitution/signing.py`
- Create: `src/trading_house/constitution/loader.py`
- Create: `tests/unit/constitution/test_signing.py`
- Create: `tests/unit/constitution/test_loader.py`
- Generate: `config/risk_constitution.public.pem`
- Generate: `config/risk_constitution.yaml.sig`
- Generate but never stage: `.local/keys/risk_constitution.private.pem`

**Interfaces:**
- Consumes: `parse_constitution_yaml(bytes)` from Task 5.
- Produces: `sign_bytes`, `verify_signature`, PEM key loaders, and `load_constitution(...) -> LoadedConstitution`.
- Produces: `LoadedConstitution(constitution, constitution_sha256, public_key_fingerprint)`.

- [ ] **Step 1: Write failing signing and loader tests**

Use an ephemeral Ed25519 key in `tmp_path`. Assert valid verification and that a
one-byte YAML change, wrong public key, malformed Base64 signature, non-Ed25519
PEM, and malformed YAML all raise the correct typed error.

Prove verification happens before parsing:

```python
def test_loader_rejects_tamper_before_yaml_parse(tmp_path: Path) -> None:
    paths = signed_fixture(tmp_path, b"version: [invalid yaml")
    paths.constitution.write_bytes(b"version: [changed invalid yaml")

    with pytest.raises(SignatureVerificationError):
        load_constitution(paths.constitution, paths.signature, paths.public_key)
```

Assert metadata hashes use lowercase 64-character SHA-256 hex values.

- [ ] **Step 2: Run tests and verify missing signing behavior**

Run: `uv run pytest tests/unit/constitution/test_signing.py tests/unit/constitution/test_loader.py -q --no-cov`

Expected: collection ERROR for missing modules.

- [ ] **Step 3: Implement Ed25519 helpers and load ordering**

```python
@dataclass(frozen=True, slots=True)
class LoadedConstitution:
    constitution: Constitution
    constitution_sha256: str
    public_key_fingerprint: str


def load_constitution(yaml_path: Path, signature_path: Path, public_key_path: Path) -> LoadedConstitution:
    yaml_bytes = yaml_path.read_bytes()
    signature = base64.b64decode(signature_path.read_bytes().strip(), validate=True)
    public_key = load_public_key(public_key_path.read_bytes())
    verify_signature(public_key, signature, yaml_bytes)
    constitution = parse_constitution_yaml(yaml_bytes)
    raw_public_key = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return LoadedConstitution(
        constitution=constitution,
        constitution_sha256=hashlib.sha256(yaml_bytes).hexdigest(),
        public_key_fingerprint=hashlib.sha256(raw_public_key).hexdigest(),
    )
```

Enforce exactly 64 decoded signature bytes and Ed25519 key types. Preserve
dependency exceptions only as chained causes; do not echo key bytes or file
contents.

- [ ] **Step 4: Verify the focused suite**

Run: `uv run pytest tests/unit/constitution -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Generate the bootstrap keypair and committed signature**

Run a reviewed one-off Python command using the implemented helpers to create an
unencrypted PKCS8 private key at `.local/keys/risk_constitution.private.pem`, its
SubjectPublicKeyInfo public key at `config/risk_constitution.public.pem`, and the
Base64 signature at `config/risk_constitution.yaml.sig`. Confirm `.local/` is
ignored before generation.

The helpers used by the command are
`generate_key_pair(private_path: Path, public_path: Path) -> None` and
`sign_file(constitution_path: Path, private_path: Path, signature_path: Path) -> None`.

Run:

```powershell
uv run python -c "from pathlib import Path; from trading_house.constitution.signing import generate_key_pair, sign_file; private=Path('.local/keys/risk_constitution.private.pem'); public=Path('config/risk_constitution.public.pem'); generate_key_pair(private, public); sign_file(Path('config/risk_constitution.yaml'), private, Path('config/risk_constitution.yaml.sig'))"
```

Run: `git check-ignore .local/keys/risk_constitution.private.pem`

Expected: the private-key path is printed, proving Git ignores it.

Run the loader against the committed three-file set and expect exit `0`.

- [ ] **Step 6: Commit only public verification artifacts**

```powershell
git add src/trading_house/constitution config/risk_constitution.public.pem config/risk_constitution.yaml.sig tests/unit/constitution
git diff --cached --name-only
git commit -m "feat: verify signed risk constitution"
```

Expected staged names do not include `.local/` or any private key.

---

### Task 7: Canonical Audit Events and Hash Primitives

**Files:**
- Create: `src/trading_house/audit/__init__.py`
- Create: `src/trading_house/audit/models.py`
- Create: `src/trading_house/audit/canonical.py`
- Create: `tests/unit/audit/test_canonical.py`
- Create: `tests/unit/audit/test_models.py`

**Interfaces:**
- Produces: `AuditEvent`, `AuditRecord`, `IntegrityReport`.
- Produces: `canonicalize_event(event) -> bytes` and `compute_entry_hash(sequence_number, previous_hash, canonical_event) -> bytes`.
- Produces constants: `DOMAIN_SEPARATOR = b"trading-house:audit:v1"` and `GENESIS_HASH = bytes(32)`.

- [ ] **Step 1: Write failing canonicalization/hash tests**

```python
def test_canonicalization_is_independent_of_payload_key_order(event_factory) -> None:
    left = event_factory(payload={"b": 2, "a": 1})
    right = event_factory(payload={"a": 1, "b": 2}, event_id=left.event_id)
    assert canonicalize_event(left) == canonicalize_event(right)


def test_hash_preimage_matches_protocol(event_factory) -> None:
    event_bytes = canonicalize_event(event_factory())
    expected = hashlib.sha256(
        DOMAIN_SEPARATOR + struct.pack(">q", 1) + GENESIS_HASH + event_bytes
    ).digest()
    assert compute_entry_hash(1, GENESIS_HASH, event_bytes) == expected
```

Also test negative/zero sequence rejection, non-32-byte previous hashes,
non-finite payload numbers, naive occurrence time, frozen records, and structured
valid/invalid `IntegrityReport` instances.

- [ ] **Step 2: Run tests and verify missing audit package**

Run: `uv run pytest tests/unit/audit -q --no-cov`

Expected: collection ERROR for missing audit modules.

- [ ] **Step 3: Implement event models and RFC 8785 serialization**

Use Pydantic's `JsonValue` for payload values. Require UUID event IDs, UTC
occurrence times, non-empty event/actor fields, and optional UUID correlation and
causation IDs. Call `rfc8785.dumps(event.model_dump(mode="json"))` and translate
canonicalization failures to `SchemaValidationError`.

Use `struct.pack(">q", sequence_number)` so Python matches PostgreSQL
`int8send`. Reject sequences outside signed 64-bit positive range.

- [ ] **Step 4: Verify audit primitives**

Run: `uv run pytest tests/unit/audit -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/trading_house/audit tests/unit/audit
git commit -m "feat: define canonical audit hash protocol"
```

---

### Task 8: PostgreSQL Roles, Local Service, and Initial Migration

**Files:**
- Create: `compose.yaml`
- Create: `.env.example`
- Create: `docker/postgres/init-roles.sh`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_audit_ledger.py`
- Create: `src/trading_house/database/__init__.py`
- Create: `src/trading_house/database/connection.py`
- Create: `src/trading_house/database/migrations.py`
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/database/test_migration.py`
- Create: `tests/integration/database/test_privileges.py`

**Interfaces:**
- Produces: `open_runtime_connection(dsn: SecretStr) -> psycopg.Connection` with UTC session state.
- Produces: `assert_at_head(connection, alembic_config) -> None`.
- Produces database function: `audit.append_event(bytea, jsonb) -> audit.ledger`.

- [ ] **Step 1: Write failing PostgreSQL migration and privilege tests**

Use `testcontainers.postgres.PostgresContainer("postgres:18-alpine")` in a
session fixture. Create `trading_house_owner NOLOGIN`, a login migrator that can
`SET ROLE trading_house_owner`, and a separate runtime login. Apply Alembic with
the migrator.

Assert tables/functions exist, database timezone is UTC, `assert_at_head`
passes, and runtime privileges satisfy:

```python
assert has_table_privilege(runtime, "audit.ledger", "SELECT")
assert not has_table_privilege(runtime, "audit.ledger", "INSERT")
assert not has_table_privilege(runtime, "audit.ledger", "UPDATE")
assert not has_table_privilege(runtime, "audit.ledger", "DELETE")
assert not has_table_privilege(runtime, "audit.ledger", "TRUNCATE")
assert has_function_privilege(runtime, "audit.append_event(bytea,jsonb)", "EXECUTE")
```

Add tests that a missing or unexpected `alembic_version` raises
`MigrationMismatchError`.

- [ ] **Step 2: Run integration tests and observe missing infrastructure**

Run: `uv run pytest tests/integration/database -m integration -q --no-cov`

Expected: collection ERROR for missing database modules/configuration.

- [ ] **Step 3: Implement Compose and database role bootstrap**

Use `postgres:18-alpine`, a named volume, healthcheck with `pg_isready`, and
environment-variable passwords. The LF-only init script must create, only when
absent:

```sql
SELECT 'CREATE ROLE trading_house_owner NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_house_owner')
\gexec
SELECT format(
  'CREATE ROLE trading_house_migrator LOGIN PASSWORD %L',
  :'migration_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_house_migrator')
\gexec
GRANT trading_house_owner TO trading_house_migrator;
SELECT format(
  'CREATE ROLE trading_house_runtime LOGIN PASSWORD %L',
  :'runtime_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_house_runtime')
\gexec
GRANT CONNECT, CREATE ON DATABASE trading_house TO trading_house_owner;
GRANT CONNECT ON DATABASE trading_house TO trading_house_runtime;
```

Use `psql -v` plus `format('%L', :'variable')`/`\gexec`; never interpolate a
password into a shell-built SQL string. `.env.example` names the variables and
uses clearly labelled, non-production development values.

- [ ] **Step 4: Implement connection and revision checking**

`open_runtime_connection` reads the secret only when calling `psycopg.connect`,
sets `TIME ZONE 'UTC'`, checks `SHOW TIME ZONE`, and wraps connection failures in
`DatabaseUnavailableError("database connection failed")`.

`assert_at_head` compares the single database revision with
`ScriptDirectory.from_config(config).get_current_head()`; no call to
`command.upgrade` may exist in runtime source.

- [ ] **Step 5: Implement migration `0001_audit_ledger`**

The upgrade must:

1. `SET ROLE trading_house_owner`.
2. Create locked schemas `audit` and `audit_crypto`.
3. Install `pgcrypto` into `audit_crypto`.
4. Create `audit.ledger` with `BIGINT sequence_number` primary key, unique UUID
   event ID, `BYTEA canonical_event`, `JSONB event_json`, 32-byte previous/entry
   hash checks, and UTC receipt time.
5. Create update/delete and truncate rejection triggers.
6. Create `audit.append_event` as `SECURITY DEFINER SET search_path = pg_catalog`.
7. Revoke all default `PUBLIC` privileges, then grant runtime schema use,
   function execution, and ledger select only.
8. Grant runtime select on `public.alembic_version` after Alembic creates it.

The append function must use `pg_advisory_xact_lock`, `max(sequence_number)+1`
under that lock rather than a non-transactional sequence, a zeroed 32-byte
genesis hash, `audit_crypto.digest`, and fully schema-qualified names. It must
check that `convert_from(p_event_bytes, 'UTF8')::jsonb = p_event_json` before
inserting.

The downgrade removes the function, triggers, table, schemas, and grants in the
reverse dependency order.

- [ ] **Step 6: Verify migrations and privileges**

Run: `uv run pytest tests/integration/database -m integration -q --no-cov`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add compose.yaml .env.example docker alembic.ini migrations src/trading_house/database tests/integration
git commit -m "feat: create locked PostgreSQL audit schema"
```

---

### Task 9: Transactional PostgreSQL Audit Repository

**Files:**
- Create: `src/trading_house/audit/repository.py`
- Create: `tests/integration/audit/test_repository.py`
- Modify: `tests/integration/conftest.py`

**Interfaces:**
- Consumes: audit models/canonical bytes from Task 7 and runtime connections from Task 8.
- Produces: `PostgresAuditLedger.append(event: AuditEvent) -> AuditRecord`.
- Produces: `PostgresAuditLedger.records() -> tuple[AuditRecord, ...]` ordered by sequence.

- [ ] **Step 1: Write failing repository integration tests**

Test one append, duplicate UUID rejection, rollback on malformed bytes, and 20
parallel appends made with distinct runtime connections. The concurrency test
must assert exactly sequences `1..20`, one genesis row, correct previous-hash
links, and no gaps.

```python
def test_concurrent_appends_form_one_continuous_chain(ledger_factory, event_factory) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda i: ledger_factory().append(event_factory(i)), range(20)))

    ordered = sorted(records, key=lambda record: record.sequence_number)
    assert [record.sequence_number for record in ordered] == list(range(1, 21))
    assert ordered[0].previous_hash == GENESIS_HASH
    assert all(right.previous_hash == left.entry_hash for left, right in pairwise(ordered))
```

- [ ] **Step 2: Run focused integration tests and observe missing repository**

Run: `uv run pytest tests/integration/audit/test_repository.py -m integration -q --no-cov`

Expected: collection ERROR because `audit.repository` does not exist.

- [ ] **Step 3: Implement repository mapping and transactions**

Define a connection-factory protocol returning a new connection per operation.
In `append`, canonicalize once, pass bytes plus `Jsonb(event.model_dump(mode="json"))`
to `SELECT * FROM audit.append_event(%s, %s)`, map the returned row to
`AuditRecord`, and commit only through the connection context. Translate unique,
data, permission, and connection errors to a redacted `AuditAppendError`.

`records()` performs an ordered read in a short read-only transaction, builds an
immutable tuple, and closes the connection before returning it, avoiding idle
transactions.

- [ ] **Step 4: Verify append behavior and concurrency**

Run: `uv run pytest tests/integration/audit/test_repository.py -m integration -q --no-cov`

Expected: PASS with 20 continuous concurrent rows.

- [ ] **Step 5: Commit**

```powershell
git add src/trading_house/audit/repository.py tests/integration
git commit -m "feat: append audit events transactionally"
```

---

### Task 10: Independent Audit-Chain Verification

**Files:**
- Create: `src/trading_house/audit/verification.py`
- Create: `tests/unit/audit/test_verification.py`
- Create: `tests/integration/audit/test_tamper_detection.py`

**Interfaces:**
- Consumes: `AuditRecord`, canonicalization, hash primitives, and ordered repository records.
- Produces: `verify_records(records: Iterable[AuditRecord]) -> IntegrityReport`.
- Produces: `PostgresAuditLedger.verify() -> IntegrityReport`.

- [ ] **Step 1: Write failing pure verification tests**

Construct immutable records and assert: empty is valid; a valid three-record
chain is valid; the first sequence must be 1; gaps, duplicate IDs, mismatched
canonical JSON, wrong previous hash, wrong entry hash, and naive receipt times
return an invalid report naming the first bad sequence and a stable reason code.

```python
def test_verifier_reports_first_broken_hash(valid_records: list[AuditRecord]) -> None:
    valid_records[1] = valid_records[1].model_copy(update={"entry_hash": b"x" * 32})
    report = verify_records(valid_records)
    assert not report.valid
    assert report.first_invalid_sequence == 2
    assert report.reason == "entry_hash_mismatch"
```

- [ ] **Step 2: Run pure tests and observe missing verifier**

Run: `uv run pytest tests/unit/audit/test_verification.py -q --no-cov`

Expected: collection ERROR for missing verification module.

- [ ] **Step 3: Implement deterministic first-failure verification**

Iterate once in ascending order, maintain expected sequence/hash and seen UUIDs,
parse canonical bytes as UTF-8 JSON, compare parsed JSON with stored JSON, require
`canonicalize_event(AuditEvent.model_validate(parsed_json)) == canonical_event`,
and recompute the entry hash.
Return `IntegrityReport(valid=True, checked_entries=n)` only after EOF.

- [ ] **Step 4: Add administrative tamper integration tests**

Using a dedicated test-superuser connection that is never passed to runtime
code, temporarily disable the mutation trigger and alter a payload/hash/delete a
row. Restore the trigger in `finally`. Assert each corruption is detected and
`health` cannot later report ready.

- [ ] **Step 5: Verify unit and integration suites**

Run: `uv run pytest tests/unit/audit tests/integration/audit -q --no-cov`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/trading_house/audit/verification.py src/trading_house/audit/repository.py tests/unit/audit tests/integration/audit
git commit -m "feat: verify PostgreSQL audit integrity"
```

---

### Task 11: Settings, Migration Gate, and Health Service

**Files:**
- Create: `src/trading_house/settings.py`
- Create: `src/trading_house/ops/__init__.py`
- Create: `src/trading_house/ops/health.py`
- Create: `tests/unit/ops/test_health.py`
- Create: `tests/integration/ops/test_health.py`

**Interfaces:**
- Produces: `RuntimeSettings` with `SecretStr` DSN and explicit constitution paths.
- Produces: `HealthService.run() -> HealthReport`.
- Consumes: verified constitution, migration check, audit verification, audit append, and `Clock`.

- [ ] **Step 1: Write failing ordered-gate tests with fakes**

Record calls in simple fakes and assert exact order:

```python
assert calls == [
    "constitution.verify",
    "database.connect",
    "database.revision",
    "audit.verify",
    "audit.append:startup",
    "audit.append:constitution_loaded",
]
```

Parameterize each stage to fail and assert no later call occurs. Verify a broken
audit report raises `AuditIntegrityError`, a revision mismatch remains typed, and
the report never contains the secret DSN.

- [ ] **Step 2: Run tests and observe missing ops/settings**

Run: `uv run pytest tests/unit/ops/test_health.py -q --no-cov`

Expected: collection ERROR for missing modules.

- [ ] **Step 3: Implement environment settings and health orchestration**

Use pydantic-settings with prefix `TRADING_HOUSE_`, `extra="ignore"`, no checked-in
`.env` auto-loading, and `SecretStr` for the runtime DSN. Paths default only to
the three checked-in public constitution artifacts; risk limits never default.

Implement `HealthCheck`, frozen `HealthReport`, and the exact fail-fast order.
The two audit events include the constitution version, YAML SHA-256, public-key
fingerprint, application version, and current UTC time. Do not catch typed
security errors inside `HealthService.run`.

- [ ] **Step 4: Run unit then real PostgreSQL health tests**

Run: `uv run pytest tests/unit/ops/test_health.py tests/integration/ops/test_health.py -q --no-cov`

Expected: PASS; the integration test appends exactly two valid audit rows and a
subsequent chain verification succeeds.

- [ ] **Step 5: Prove runtime source cannot migrate**

Run: `rg -n "command\.upgrade|alembic.*upgrade" src/trading_house`

Expected: no matches.

- [ ] **Step 6: Commit**

```powershell
git add src/trading_house/settings.py src/trading_house/ops tests/unit/ops tests/integration/ops
git commit -m "feat: gate readiness on verified foundations"
```

---

### Task 12: Operator CLI and Stable Failure Codes

**Files:**
- Create: `src/trading_house/cli.py`
- Create: `tests/unit/test_cli.py`
- Create: `tests/integration/test_cli.py`

**Interfaces:**
- Produces commands: `constitution verify`, `constitution sign`, `db check`, `audit verify`, and `health`.
- Consumes: loaders, signing helpers, settings, migration checks, ledger, and health service.

- [ ] **Step 1: Write failing CLI tests**

Use `typer.testing.CliRunner`. Assert command help, successful JSON output, exact
typed exit codes, and redaction. The sign command must require explicit
`--constitution`, `--private-key`, and `--signature-output` paths and must refuse
to overwrite an existing signature without `--force`.

```python
def test_signature_failure_uses_stable_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(cli, "load_constitution", raise_signature_error)
    result = runner.invoke(app, ["constitution", "verify"])
    assert result.exit_code == ExitCode.SIGNATURE
    assert "PRIVATE" not in result.stdout
```

- [ ] **Step 2: Run tests and observe missing CLI**

Run: `uv run pytest tests/unit/test_cli.py -q --no-cov`

Expected: collection ERROR because `trading_house.cli` does not exist.

- [ ] **Step 3: Implement thin commands and centralized error mapping**

Add the console entry point only when `src/trading_house/cli.py` exists:

```toml
[project.scripts]
trading-house = "trading_house.cli:app"
```

Commands construct dependencies, call one public service, and render either a
small text result or deterministic JSON. A single wrapper maps typed errors to
`typer.Exit(code=...)`. Unexpected errors return exit `1` with a correlation ID,
not a traceback or exception payload, unless an explicit developer debug flag is
set.

The sign command reads a PEM private key only for the duration of the command,
signs exact bytes, writes Base64 plus one newline via an atomic same-directory
temporary file and `Path.replace`, and drops references before exit.

- [ ] **Step 4: Verify unit and PostgreSQL CLI paths**

Run: `uv run pytest tests/unit/test_cli.py tests/integration/test_cli.py -q --no-cov`

Expected: PASS.

Run: `uv run trading-house constitution verify`

Expected: exit `0` and output includes constitution version/hash but no private
material.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml uv.lock src/trading_house/cli.py tests/unit/test_cli.py tests/integration/test_cli.py
git commit -m "feat: add foundation operator commands"
```

---

### Task 13: Property and Architectural Invariant Tests

**Files:**
- Create: `tests/property/test_timestamps.py`
- Create: `tests/property/test_constitution_signatures.py`
- Create: `tests/property/test_canonical_audit.py`
- Create: `tests/property/test_schema_boundaries.py`
- Create: `tests/acceptance/test_architecture.py`

**Interfaces:**
- Consumes all Phase 0 public contracts.
- Produces executable guards for invariants I-2 and I-10 and Phase 0's no-trading boundary.

- [ ] **Step 1: Write property tests before correcting uncovered behavior**

Use Hypothesis strategies that generate aware offsets, ordered timestamp triples,
one-byte YAML mutations, reordered nested JSON objects, event sequences, and
unexpected schema fields.

Required properties:

```python
@given(aware_datetimes())
def test_utc_normalization_preserves_instant(value: datetime) -> None:
    assert ensure_utc(value).timestamp() == value.timestamp()
    assert ensure_utc(value).tzinfo is timezone.utc


@given(binary(min_size=1), integers(min_value=0))
def test_any_single_byte_mutation_breaks_signature(data: bytes, index_seed: int) -> None:
    signature, public_key = sign_fixture(data)
    changed = mutate_one_byte(data, index_seed)
    with pytest.raises(SignatureVerificationError):
        verify_signature(public_key, signature, changed)
```

The architecture test walks `src/trading_house/**/*.py` with `ast` and fails on
imports whose top-level package is `MetaTrader5`, `langgraph`, `openai`,
`anthropic`, or `ccxt`. It also fails if a module outside `constitution/signing.py`
loads an Ed25519 private key or if runtime source invokes Alembic upgrade.

- [ ] **Step 2: Run property/architecture tests and confirm any real gaps**

Run: `uv run pytest tests/property tests/acceptance/test_architecture.py -q --no-cov`

Expected: FAIL only where generated cases expose a missing boundary; record the
exact failing example from Hypothesis.

- [ ] **Step 3: Make the minimal production corrections for failing properties**

For each failure, change only the responsible validator/helper. Do not weaken a
property or suppress a health/security exception. Add an example regression test
for each minimized Hypothesis counterexample.

- [ ] **Step 4: Verify property and full unit suites**

Run: `uv run pytest tests/unit tests/property tests/acceptance/test_architecture.py -q --no-cov`

Expected: PASS with no Hypothesis health-check suppressions.

- [ ] **Step 5: Commit**

```powershell
git add src tests/property tests/acceptance
git commit -m "test: enforce phase 0 safety invariants"
```

---

### Task 14: Fresh-Checkout Acceptance, CI, and Operator Documentation

**Files:**
- Create: `tests/acceptance/test_phase0.py`
- Create: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `.env.example`

**Interfaces:**
- Produces: reproducible local and CI verification commands.
- Produces: documented Phase 0-to-Phase 1 handoff.

- [ ] **Step 1: Write the failing Phase 0 acceptance test**

The PostgreSQL-backed acceptance test must apply migrations with migrator
credentials, load the checked-in constitution, append a fixture event with the
runtime role, verify the chain, run `HealthService`, verify the resulting chain,
and assert no forbidden imports. It must fail if the committed signature or
public key is missing.

Run: `uv run pytest tests/acceptance/test_phase0.py -m integration -q --no-cov`

Expected: FAIL until CI fixtures and the end-to-end wiring are complete.

- [ ] **Step 2: Add Linux and Windows CI jobs**

Linux must install from `uv.lock`, run PostgreSQL 18, create the three database
roles, apply migrations, then run:

```text
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

Windows must run the same lock, format, lint, typing, unit, property, and
architecture checks while excluding tests marked `integration`. CI receives no
private signing key.

- [ ] **Step 3: Write exact operator documentation**

Document prerequisites, `uv sync --locked --all-groups`, copying `.env.example`
to `.env`, Docker Compose startup, migration commands, constitution verification
and offline signing, database/audit checks, health, all test/quality commands,
database authority separation, private-key handling, and recovery from each
typed startup failure.

State prominently that Phase 0 cannot trade and that Phase 1 may start only
after this acceptance suite is green.

- [ ] **Step 4: Run fresh full verification**

Run: `uv lock --check`

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`

Run: `uv run pytest`

Run: `docker compose config --quiet`

Run: `uv run trading-house constitution verify`

Run: `rg -n "^(from|import) (MetaTrader5|langgraph|openai|anthropic|ccxt)" src tests`

Expected: every command exits `0`; pytest reports zero failures and at least 95%
coverage; the forbidden-import scan prints no matches.

- [ ] **Step 5: Review Phase 0 requirements line by line**

Confirm every included item and Definition of Done statement in
`docs/superpowers/specs/2026-08-03-phase-0-foundation-design.md` maps to a passing
test, command, migration, or documented operator step. Add a missing acceptance
assertion before continuing if any statement lacks evidence.

- [ ] **Step 6: Commit the accepted foundation**

```powershell
git add .github README.md pyproject.toml .env.example tests/acceptance
git commit -m "docs: complete phase 0 acceptance workflow"
```

---

## Final Verification Gate

After Task 14, run these commands again from the repository root without relying
on previous output:

```powershell
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
docker compose config --quiet
uv run trading-house constitution verify
git status --short
```

Expected results:

- all quality and test commands exit `0`;
- pytest reports no failures and coverage is at least 95%;
- constitution verification reports version `1` and stable hashes;
- Git status contains no accidental private key, `.env`, database volume, or
  unrelated generated artifact;
- no claim that Phase 0 can trade, connect to MT5, or generate profit is made.
