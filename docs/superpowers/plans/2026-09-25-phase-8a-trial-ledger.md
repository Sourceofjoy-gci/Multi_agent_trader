# Phase 8A — Canonical Evidence and Trial Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the durable, append-only trial evidence foundation that preregisters candidate families, seals immutable result bundles, verifies them, and imports the three completed Phase 7 results as explicitly legacy evidence.

**Architecture:** A frozen `TrialProtocol` seals a complete candidate tuple in one PostgreSQL event. A content-addressed JSON evidence store owns result bytes; PostgreSQL owns registration order, event identity, and a server-computed hash chain. `research trial` is the only new CLI surface. The existing `Trial`, `TrialStatus`, and `deflation_trial_count` remain compatible; the ledger protocol grows append/replay methods rather than introducing a second ledger.

**Tech Stack:** Python 3.12, Pydantic v2 strict/frozen models, PostgreSQL 18, `psycopg` 3, Alembic, Typer, stdlib `hashlib`/`json`/`tempfile`, pytest, Hypothesis, Ruff, strict mypy.

## Global Constraints

- Target Python `>=3.12,<3.13`.
- Use `CanonicalModel` semantics: `strict=True`, `frozen=True`, `extra="forbid"`.
- Use `Decimal` for money, prices, rates, and digests' decimal inputs; use strings for canonical Decimal output.
- Every canonical timestamp is timezone-aware UTC and serializes with a `Z` suffix.
- Use absolute imports everywhere; the architecture suite rejects relative imports.
- Do not add NumPy or any other dependency in 8A. NumPy is introduced by 8C when statistical arrays are implemented.
- Do not edit `migrations/versions/0002_memory_and_trials.py`; the new ledger lives beside `research.trials`, not by rewriting applied history.
- Do not import `trading_house.audit` into the research evidence chain. The audit ledger is a separate chain.
- Evidence files are content-addressed and written atomically; an existing digest is never overwritten with different bytes.
- The application runtime never owns the ledger tables, never calls Alembic, and receives no direct event `INSERT`, `UPDATE`, `DELETE`, or `TRUNCATE` privilege.
- PostgreSQL sequence gaps caused by rolled-back transactions are allowed; integrity uses explicit previous-hash links.
- All new database behavior has a real-PostgreSQL integration test; unit tests do not substitute for it.
- Every task ends with a focused commit. Do not commit Phase 7 JSON artifacts, a local database, a DSN, or evidence-root contents.
- Run the focused test first, then the repository quality gates listed in Task 7.

---

## File Map

### Create

- `src/trading_house/research/canonical.py` — model-to-canonical-bytes and SHA-256 helpers. It is separate from `audit/canonical.py` because RFC 8785's number rules do not implement the Decimal/timestamp contract in the Phase 8 spec.
- `src/trading_house/research/evidence.py` — `EvidenceBundle`, return-series and cost-status enums, `EvidenceStore`, atomic content-addressed writes, and bundle verification.
- `src/trading_house/research/ledger_store.py` — concrete `PostgresTrialLedger` append/read/replay/verify/counters implementation and the evolved `TrialLedger` protocol.
- `src/trading_house/research/legacy_import.py` — Phase 7 outer-artifact parsing, original-digest verification, realized daily-return derivation, and idempotent `LEGACY_IMPORTED` events.
- `src/trading_house/ops/ledger.py` — composition root for the ledger DSN and evidence root, keeping `cli.py` from owning persistence wiring.
- `migrations/versions/0007_trial_ledger_events.py` — `research.trial_ledger_events`, `research.trial_ledger_heads`, append function, append-only triggers, grants, and downgrade.
- `tests/unit/research/test_evidence.py` — canonical bundle and evidence-store unit tests.
- `tests/unit/research/test_ledger_events.py` — event, payload, and counter unit tests.
- `tests/property/test_trial_evidence.py` — Hypothesis invariants for canonical bytes and chain hashes.
- `tests/integration/research/test_trial_ledger_store.py` — real PostgreSQL append, concurrency, privilege, chain, and evidence-reference tests.
- `tests/integration/research/test_trial_cli.py` — real-PostgreSQL CLI round trips for all six trial commands.
- `tests/acceptance/test_phase8a.py` — 8A acceptance checks that do not depend on machine-specific temporary artifacts.

### Modify

- `src/trading_house/research/trial_ledger.py` — add protocol/trial/event/counter contracts while preserving the current public symbols.
- `src/trading_house/research/__init__.py` — re-export the new public ledger and evidence contracts.
- `src/trading_house/settings.py` — add optional `research_ledger_dsn` and durable `evidence_root` settings.
- `src/trading_house/core/errors.py` — add typed ledger/evidence errors and stable exit codes.
- `src/trading_house/cli.py` — add the `research trial` command group and composition helpers.
- `docker/postgres/init-roles.sh` and `compose.yaml` — create the dedicated `trading_house_research` database on a fresh cluster.
- `.env.example` — document the research DSN and evidence root.
- `tests/conftest.py` — provision and migrate a second database, add research DSNs, and add an isolated research-ledger fixture.
- `tests/unit/test_cli.py` — include `research` in the command-group contract and test the new error exit codes.
- `tests/unit/test_settings.py` — clean and test the two new environment suffixes.
- `tests/property/test_schema_boundaries.py` — import new models and add builders for every new datetime-bearing model.
- `tests/acceptance/test_architecture.py` — add a research-root import guard and its positive/negative guard tests.
- `README.md` — document the second database, evidence root, six commands, migration, and Phase 7 legacy import.
- `tests/acceptance/test_phase7.py` — no change; its three pinned result digests remain the import fixture values.

### Explicitly unchanged

- `pyproject.toml` — no 8A dependency change.
- `src/trading_house/research/backtest/*` — no backtest import-allowlist widening is needed because 8A modules live above `research/backtest/`.
- `src/trading_house/audit/*` — separate chain and canonical format.
- `migrations/versions/0002_memory_and_trials.py` — applied history.
- `tests/property/test_schema_boundaries.py`'s existing model cases remain unchanged.

---

### Task 1: Add the frozen protocol, event, and counter contracts

**Files:**
- Create: `src/trading_house/research/canonical.py`
- Modify: `src/trading_house/research/trial_ledger.py`
- Modify: `src/trading_house/research/__init__.py`
- Test: `tests/unit/research/test_trial_ledger.py`
- Test: `tests/unit/research/test_ledger_events.py`

**Interfaces:**
- Produces `canonical_bytes(model: BaseModel) -> bytes` and `canonical_sha256(model: BaseModel) -> str`.
- Produces `TrialProtocol`, `TrialSpec`, `DataSpec`, `ExecutionSpec`, `CostSpec`, `ValidationSpec`, `RegimeSpec`, `HoldoutSpec`, `LedgerEvent`, `LedgerEventType`, `ScopeKind`, `RegistrationState`, `HoldoutState`, `ReturnSeriesBasis`, `CostAttributionStatus`, `TrialCounters`, `LedgerRecord`, `LedgerIntegrityReport`, and `trial_counters(events)`.
- Preserves `Trial`, `TrialStatus`, `deflation_trial_count`, and the import path used by the current unit tests.

- [ ] **Step 1: Write failing canonicalization and protocol tests**

Add to `tests/unit/research/test_ledger_events.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.marketdata.models import Timeframe
from trading_house.research.canonical import canonical_bytes, canonical_sha256
from trading_house.research.trial_ledger import (
    CostSpec,
    DataSpec,
    ExecutionSpec,
    HoldoutSpec,
    HoldoutState,
    LedgerEventType,
    RegistrationState,
    RegimeSpec,
    ScopeKind,
    TrialProtocol,
    TrialSpec,
    ValidationSpec,
)
from trading_house.research.backtest.costs import CostModel


def _data() -> DataSpec:
    return DataSpec(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M15,
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 6, 1, tzinfo=UTC),
        dataset_sha256="a" * 64,
        point_in_time_policy="availability_time",
    )


def _candidate(index: int) -> TrialSpec:
    return TrialSpec(
        trial_id=f"trial-{index}",
        spec_id=f"spec-{index}",
        rationale="declared before execution",
        parameter_space=(("window", f"value-{index}"),),
    )


def _protocol(candidates: tuple[TrialSpec, ...] | None = None) -> TrialProtocol:
    return TrialProtocol(
        protocol_id="protocol-1",
        protocol_version="1",
        agent_run_id="run-1",
        strategy_id="session_momentum",
        strategy_version="1",
        strategy_sha256="b" * 64,
        data=_data(),
        execution=ExecutionSpec(
            seed="fixed",
            warmup_bars=20,
            fill_policy="pessimistic-bar",
            sizing_policy="risk-engine",
        ),
        costs=CostSpec(
            baseline=CostModel(
                commission_per_lot_per_side=Decimal("0"),
                slippage_points_per_side=Decimal("0.4"),
                swap_long_points_per_day=Decimal("-7.7"),
                swap_short_points_per_day=Decimal("2"),
                triple_swap_weekday=2,
                stress_multiplier=Decimal("1"),
            ),
            stress_multipliers=(Decimal("1.5"), Decimal("2")),
        ),
        validation=ValidationSpec(
            primary_metric="net_expectancy",
            wfa_train_months=24,
            wfa_validation_months=6,
            wfa_test_months=6,
            purge_hours=16,
            embargo_hours=16,
            cpcv_folds=6,
            bootstrap_replicates=10000,
            bootstrap_c=Decimal("6.7"),
            trial_count_rule="conservative-selection-lotteries",
        ),
        regimes=RegimeSpec(labels=("london", "new_york"), provenance_sha256="c" * 64),
        holdout=HoldoutSpec(state=HoldoutState.NOT_DEFINED),
        candidates=candidates if candidates is not None else (_candidate(1), _candidate(2)),
    )


def test_canonical_bytes_are_sorted_compact_utf8_and_reject_nan() -> None:
    protocol = _protocol()

    encoded = canonical_bytes(protocol)

    assert encoded.startswith(b'{"agent_run_id":"run-1"')
    assert b", " not in encoded
    assert canonical_sha256(protocol) == canonical_sha256(protocol)


def test_protocol_requires_a_non_empty_unique_candidate_family() -> None:
    with pytest.raises(ValidationError, match="candidate"):
        _protocol(())

    with pytest.raises(ValidationError, match="unique"):
        _protocol((_candidate(1), _candidate(1)))


def test_protocol_costs_must_declare_the_signed_stress_grid() -> None:
    with pytest.raises(ValidationError, match="stress"):
        CostSpec(
            baseline=_protocol().costs.baseline,
            stress_multipliers=(Decimal("1.5"),),
        )


def test_event_enums_have_the_closed_wire_values() -> None:
    assert LedgerEventType.PREREGISTERED.value == "preregistered"
    assert ScopeKind.PROTOCOL.value == "protocol"
    assert RegistrationState.PROSPECTIVE.value == "prospective"
```

Use the existing `_trial` helper in `tests/unit/research/test_trial_ledger.py` unchanged for the five existing assertions. Add one test that `deflation_trial_count` still counts a failed trial, and one test that a `Trial` with the same fields still validates.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```powershell
uv run pytest tests/unit/research/test_ledger_events.py tests/unit/research/test_trial_ledger.py -q --no-cov
```

Expected: collection or import failure because `research.canonical` and the new contracts do not exist yet.

- [ ] **Step 3: Implement canonical bytes**

Create `src/trading_house/research/canonical.py`:

```python
"""Canonical JSON bytes for research evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from trading_house.core.errors import SchemaValidationError


def canonical_bytes(model: BaseModel) -> bytes:
    try:
        payload: Any = model.model_dump(mode="json")
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise SchemaValidationError() from error


def canonical_sha256(model: BaseModel) -> str:
    return hashlib.sha256(canonical_bytes(model)).hexdigest()
```

- [ ] **Step 4: Implement the protocol and event models**

Extend `src/trading_house/research/trial_ledger.py` with strict frozen models. Import `ensure_utc` from `trading_house.core.clock`, `TimestampError` from `trading_house.core.errors`, `JsonValue` from `pydantic`, `Timeframe` from `trading_house.marketdata.models`, `CostModel` from `trading_house.research.backtest.costs`, and the existing value types. Use these exact field names and wire values:

```python
class ScopeKind(str, Enum):  # noqa: UP042
    PROTOCOL = "protocol"
    TRIAL = "trial"
    ATTEMPT = "attempt"


class LedgerEventType(str, Enum):  # noqa: UP042
    PREREGISTERED = "preregistered"
    LEGACY_IMPORTED = "legacy_imported"
    EXECUTION_STARTED = "execution_started"
    RESULT_RECORDED = "result_recorded"
    FAILED = "failed"
    EVIDENCE_SEALED = "evidence_sealed"
    VALIDATED = "validated"
    GATE_DECIDED = "gate_decided"


class RegistrationState(str, Enum):  # noqa: UP042
    PROSPECTIVE = "prospective"
    LEGACY_UNPREGISTERED = "legacy_unregistered"


class HoldoutState(str, Enum):  # noqa: UP042
    NOT_DEFINED = "not_defined"
    LOCKED = "locked"
    OPENED = "opened"
    CONSUMED = "consumed"
    CONTAMINATED = "contaminated"


class ReturnSeriesBasis(str, Enum):  # noqa: UP042
    REALIZED_CLOSED_TRADES = "realized_closed_trades"
    MARK_TO_MARKET = "mark_to_market"
    MISSING = "missing"


class CostAttributionStatus(str, Enum):  # noqa: UP042
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


def _utc(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except TimestampError as error:
        raise ValueError(str(error)) from error


class DataSpec(CanonicalModel):
    instrument_id: InstrumentId
    timeframe: Timeframe
    start: datetime
    end: datetime
    dataset_sha256: NonEmptyStr
    point_in_time_policy: NonEmptyStr

    @field_validator("start", "end")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def range_is_forward(self) -> Self:
        if self.start >= self.end:
            raise ValueError("data start must precede end")
        return self


class ExecutionSpec(CanonicalModel):
    seed: NonEmptyStr
    warmup_bars: NonNegativeInt
    fill_policy: NonEmptyStr
    sizing_policy: NonEmptyStr


class CostSpec(CanonicalModel):
    baseline: CostModel
    stress_multipliers: tuple[Decimal, ...]

    @model_validator(mode="after")
    def stress_grid_is_exact(self) -> Self:
        if set(self.stress_multipliers) != {Decimal("1.5"), Decimal("2")}:
            raise ValueError("stress grid must contain exactly 1.5 and 2")
        return self


class ValidationSpec(CanonicalModel):
    primary_metric: NonEmptyStr
    wfa_train_months: PositiveInt
    wfa_validation_months: PositiveInt
    wfa_test_months: PositiveInt
    purge_hours: NonNegativeInt
    embargo_hours: NonNegativeInt
    cpcv_folds: PositiveInt
    bootstrap_replicates: PositiveInt
    bootstrap_c: PositiveDecimal
    trial_count_rule: NonEmptyStr


class RegimeSpec(CanonicalModel):
    labels: tuple[NonEmptyStr, ...]
    provenance_sha256: NonEmptyStr

    @model_validator(mode="after")
    def labels_are_unique(self) -> Self:
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("regime labels must be unique")
        return self


class HoldoutSpec(CanonicalModel):
    state: HoldoutState
    start: datetime | None = None
    end: datetime | None = None
    dataset_sha256: NonEmptyStr | None = None

    @field_validator("start", "end")
    @classmethod
    def normalize_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def dates_match_state(self) -> Self:
        defined = self.start is not None and self.end is not None and self.dataset_sha256 is not None
        if self.state is HoldoutState.NOT_DEFINED and defined:
            raise ValueError("an undefined holdout cannot carry dates or a hash")
        if self.state in {HoldoutState.LOCKED, HoldoutState.OPENED, HoldoutState.CONSUMED} and not defined:
            raise ValueError("a defined holdout requires dates and a hash")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("holdout start must precede end")
        return self


class TrialSpec(CanonicalModel):
    trial_id: NonEmptyStr
    spec_id: NonEmptyStr
    rationale: NonEmptyStr
    parameter_space: tuple[tuple[NonEmptyStr, NonEmptyStr], ...]


class TrialProtocol(CanonicalModel):
    protocol_id: NonEmptyStr
    protocol_version: NonEmptyStr
    agent_run_id: NonEmptyStr
    strategy_id: NonEmptyStr
    strategy_version: NonEmptyStr
    strategy_sha256: NonEmptyStr
    data: DataSpec
    execution: ExecutionSpec
    costs: CostSpec
    validation: ValidationSpec
    regimes: RegimeSpec
    holdout: HoldoutSpec
    candidates: tuple[TrialSpec, ...]

    @model_validator(mode="after")
    def candidate_family_is_complete_and_unique(self) -> Self:
        if not self.candidates:
            raise ValueError("a protocol requires at least one candidate")
        ids = [candidate.trial_id for candidate in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate trial ids must be unique")
        return self
```

Update `src/trading_house/research/__init__.py` to re-export the existing seven names plus `canonical_bytes`, `canonical_sha256`, `TrialProtocol`, `TrialSpec`, `LedgerEvent`, `LedgerEventType`, `ScopeKind`, `RegistrationState`, `HoldoutState`, `ReturnSeriesBasis`, `CostAttributionStatus`, `TrialCounters`, `LedgerRecord`, and `LedgerIntegrityReport`. Keep the module's `__all__` sorted and explicit.

Define payload models with a `Literal` event type and a discriminated union:

```python
class PreregisteredPayload(CanonicalModel):
    event_type: Literal[LedgerEventType.PREREGISTERED]
    protocol: TrialProtocol
    registration_state: RegistrationState


class LegacyImportedPayload(CanonicalModel):
    event_type: Literal[LedgerEventType.LEGACY_IMPORTED]
    trial: TrialSpec
    evidence_sha256: NonEmptyStr
    source_result_sha256: NonEmptyStr
    legacy_reason: NonEmptyStr


class ExecutionStartedPayload(CanonicalModel):
    event_type: Literal[LedgerEventType.EXECUTION_STARTED]
    attempt_id: NonEmptyStr
    execution_started_at: datetime


class ResultRecordedPayload(CanonicalModel):
    event_type: Literal[LedgerEventType.RESULT_RECORDED]
    attempt_id: NonEmptyStr
    source_result_sha256: NonEmptyStr


class FailedPayload(CanonicalModel):
    event_type: Literal[LedgerEventType.FAILED]
    attempt_id: NonEmptyStr
    reason: NonEmptyStr


class EvidenceSealedPayload(CanonicalModel):
    event_type: Literal[LedgerEventType.EVIDENCE_SEALED]
    attempt_id: NonEmptyStr
    evidence_sha256: NonEmptyStr


class ValidatedPayload(CanonicalModel):
    event_type: Literal[LedgerEventType.VALIDATED]
    report_sha256: str


class GateDecidedPayload(CanonicalModel):
    event_type: Literal[LedgerEventType.GATE_DECIDED]
    decision: Literal["REJECTED", "RESEARCH_PASSED", "PAPER_APPROVED"]
    report_sha256: str


LedgerEventPayload = Annotated[
    Union[
        PreregisteredPayload,
        LegacyImportedPayload,
        ExecutionStartedPayload,
        ResultRecordedPayload,
        FailedPayload,
        EvidenceSealedPayload,
        ValidatedPayload,
        GateDecidedPayload,
    ],
    Field(discriminator="event_type"),
]
```

Define `LedgerEvent` and `TrialCounters` exactly as follows:

```python
class LedgerEvent(CanonicalModel):
    event_id: UUID
    scope_kind: ScopeKind
    scope_id: NonEmptyStr
    event_type: LedgerEventType
    trial_id: NonEmptyStr | None = None
    attempt_id: NonEmptyStr | None = None
    spec_sha256: NonEmptyStr
    occurred_at: datetime
    payload: LedgerEventPayload
    legacy: bool = False
    legacy_reason: NonEmptyStr | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def outer_type_matches_payload(self) -> Self:
        if self.event_type is not self.payload.event_type:
            raise ValueError("event type must match payload discriminator")
        if self.legacy and self.legacy_reason is None:
            raise ValueError("legacy events require a reason")
        if not self.legacy and self.legacy_reason is not None:
            raise ValueError("prospective events cannot carry a legacy reason")
        return self


class TrialCounters(CanonicalModel):
    audit_attempts: NonNegativeInt
    selection_lotteries: NonNegativeInt
    effective_specifications: NonNegativeInt


class LedgerRecord(CanonicalModel):
    sequence: PositiveInt
    event_id: UUID
    scope_kind: ScopeKind
    scope_id: NonEmptyStr
    trial_id: NonEmptyStr | None
    attempt_id: NonEmptyStr | None
    event_type: LedgerEventType
    spec_sha256: NonEmptyStr
    event_json: dict[str, JsonValue]
    payload_sha256: NonEmptyStr
    previous_hash: NonEmptyStr
    event_hash: NonEmptyStr
    legacy: bool
    legacy_reason: NonEmptyStr | None
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def normalize_recorded_at(cls, value: datetime) -> datetime:
        return _utc(value)


class LedgerIntegrityReport(CanonicalModel):
    valid: bool
    checked_events: NonNegativeInt
    reason: NonEmptyStr | None

    @model_validator(mode="after")
    def reason_matches_validity(self) -> Self:
        if self.valid and self.reason is not None:
            raise ValueError("a valid report cannot carry a failure reason")
        if not self.valid and self.reason is None:
            raise ValueError("an invalid report requires a reason")
        return self


def trial_counters(events: Iterable[LedgerEvent]) -> TrialCounters:
    started: set[str] = set()
    selection: set[str] = set()
    specifications: set[str] = set()
    for event in events:
        if event.event_type in {LedgerEventType.EXECUTION_STARTED, LedgerEventType.LEGACY_IMPORTED}:
            if event.attempt_id is not None:
                started.add(event.attempt_id)
        if event.trial_id is not None and event.event_type in {
            LedgerEventType.EXECUTION_STARTED,
            LedgerEventType.LEGACY_IMPORTED,
        }:
            selection.add(event.trial_id)
            specifications.add(event.spec_sha256)
    return TrialCounters(
        audit_attempts=len(started),
        selection_lotteries=len(selection),
        effective_specifications=len(specifications),
    )
```

`spec_sha256` is the canonical digest of the declared `TrialSpec` or `TrialProtocol`, copied onto the event by the producer. It is not the digest of the mutable event envelope.

- [ ] **Step 5: Run tests and format/type-check the task**

Run:

```powershell
uv run pytest tests/unit/research/test_ledger_events.py tests/unit/research/test_trial_ledger.py -q --no-cov
uv run ruff format src/trading_house/research/canonical.py src/trading_house/research/trial_ledger.py tests/unit/research/test_ledger_events.py
uv run ruff check src/trading_house/research/canonical.py src/trading_house/research/trial_ledger.py tests/unit/research/test_ledger_events.py
uv run mypy
```

Expected: all focused tests pass, Ruff is clean, and mypy reports no errors.

- [ ] **Step 6: Commit the contract layer**

```powershell
git add src/trading_house/research/canonical.py src/trading_house/research/trial_ledger.py src/trading_house/research/__init__.py tests/unit/research/test_trial_ledger.py tests/unit/research/test_ledger_events.py
git commit -m "feat: define the phase 8 trial ledger contracts"
```

---

### Task 2: Add canonical evidence bundles and the content-addressed store

**Files:**
- Create: `src/trading_house/research/evidence.py`
- Modify: `src/trading_house/research/__init__.py`
- Test: `tests/unit/research/test_evidence.py`
- Test: `tests/property/test_trial_evidence.py`

**Interfaces:**
- Consumes `canonical_bytes`, `canonical_sha256`, `RegistrationState`, `ReturnSeriesBasis`, `CostAttributionStatus`, and `BacktestResult`.
- Produces `DailyReturnPoint`, `CostSummary`, `EvidenceProvenance`, `EvidenceBundle`, `StoredEvidence`, and `EvidenceStore`.
- `EvidenceStore.write(bundle) -> StoredEvidence` returns the digest and root-relative path.
- `EvidenceStore.read(digest) -> EvidenceBundle` verifies bytes before parsing.
- `EvidenceStore.verify(digest) -> None` raises `EvidenceIntegrityError` for missing, altered, or non-canonical content.

- [ ] **Step 1: Write failing evidence tests**

Create `tests/unit/research/test_evidence.py` and `tests/property/test_trial_evidence.py` now, before implementation. Use the property assertions in Step 4 as the second failing test file; do not defer test authorship until after the store exists. The unit helper and the property module each carry a local `_bundle()` builder. Copy the exact construction pattern from `tests/unit/research/backtest/test_result.py:18-79`, using these concrete values:

```python
NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def _result() -> BacktestResult:
    trade = SimulatedTrade(
        proposal_id="p-1",
        side=Side.BUY,
        lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        entry_at=NOW,
        exit_price=Decimal("1.10100"),
        exit_at=NOW + timedelta(minutes=12),
        exit_kind=ExitKind.TIME,
        gross_pnl=Decimal("100"),
        commission=Decimal("7"),
        swap=Decimal("-3"),
        net_pnl=Decimal("90"),
    )
    return BacktestResult(
        run_id="run-1",
        strategy_id="strat-1",
        strategy_version="v1",
        exit_policy=NoExitPolicy(kind="none"),
        constitution_sha256="a" * 64,
        contract_sha256="b" * 64,
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        start=NOW,
        end=NOW + timedelta(hours=1),
        firm_equity=Decimal("100000"),
        cost_model=CostModel(
            commission_per_lot_per_side=Decimal("3.50"),
            slippage_points_per_side=Decimal("1"),
            swap_long_points_per_day=Decimal("-1"),
            swap_short_points_per_day=Decimal("-1"),
            triple_swap_weekday=2,
        ),
        atr_period=14,
        spread_window=20,
        defective_bar_tolerance=Fraction(0),
        trades=(trade,),
        rejections=(),
        bars_seen=60,
        snapshots_skipped=0,
        net_pnl=Decimal("90"),
    )


def _bundle(*, day_offset: int = 0) -> EvidenceBundle:
    result = _result()
    return EvidenceBundle(
        result_schema_version=1,
        trial_id="trial-1",
        attempt_id="attempt-1",
        spec_sha256="d" * 64,
        source_result_sha256=result.digest(),
        result=result,
        daily_returns=(
            DailyReturnPoint(day=NOW.date() + timedelta(days=day_offset), value=Decimal("0")),
        ),
        return_series_basis=ReturnSeriesBasis.REALIZED_CLOSED_TRADES,
        costs=CostSummary(
            status=CostAttributionStatus.PARTIAL,
            commission=Decimal("7"),
            swap=Decimal("-3"),
            spread_cost=None,
            slippage_cost=None,
        ),
        provenance=EvidenceProvenance(
            agent_run_id="run-1",
            source_artifact_sha256="e" * 64,
            dataset_sha256="f" * 64,
            registered_at=NOW,
            occurred_at=NOW,
            registration_state=RegistrationState.PROSPECTIVE,
            holdout_state=HoldoutState.NOT_DEFINED,
        ),
    )
```

Import `BacktestResult`, `CostModel`, `ExitKind`, `NoExitPolicy`, `Side`, `Timeframe`, `EvidenceBundle`, `EvidenceProvenance`, `CostSummary`, and the two evidence enums. The test must cover:

```python
def test_bundle_digest_is_the_sha256_of_canonical_bytes() -> None:
    bundle = _bundle()
    stored = EvidenceStore(tmp_path).write(bundle)

    assert stored.sha256 == canonical_sha256(bundle)
    assert stored.path == f"{stored.sha256[:2]}/{stored.sha256}.json"


def test_store_never_overwrites_different_bytes_at_an_existing_digest(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    first = store.write(_bundle())
    target = store.root / first.path
    target.write_bytes(b'{"tampered":true}\n')

    with pytest.raises(EvidenceIntegrityError):
        store.write(_bundle())


def test_store_read_rejects_a_file_whose_digest_does_not_match(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    stored = store.write(_bundle())
    (store.root / stored.path).write_bytes(b'{"changed":true}')

    with pytest.raises(EvidenceIntegrityError):
        store.read(stored.sha256)


def test_atomic_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.write(_bundle())

    assert not list(store.root.rglob("*.tmp"))
```

Add a test that a legacy bundle with `CostAttributionStatus.PARTIAL` keeps `spread_cost` and `slippage_cost` as `None`, and a test that `source_result_sha256` is the existing `BacktestResult.digest()` value.

- [ ] **Step 2: Run the tests and verify failure**

```powershell
uv run pytest tests/unit/research/test_evidence.py tests/property/test_trial_evidence.py -q --no-cov
```

Expected: import failure because `research.evidence` does not exist.

- [ ] **Step 3: Implement the evidence models and store**

Add this class to `src/trading_house/core/errors.py`:

```python
class EvidenceIntegrityError(TradingHouseError):
    public_message = "evidence integrity verification failed"
```

Add its `ExitCode` member and CLI mapping in Task 3/Task 5, where the complete exit-code table is edited. Then create `src/trading_house/research/evidence.py` with imports for `hashlib`, `os`, `tempfile`, `date`, `datetime`, `Path`, `Literal`, `PositiveInt`, `NonNegativeInt`, `Decimal`, `Self`, `model_validator`, `field_validator`, and the existing canonical/result/value types. Define:

```python
class DailyReturnPoint(CanonicalModel):
    day: date
    value: Decimal


class CostSummary(CanonicalModel):
    status: CostAttributionStatus
    commission: Decimal
    swap: Decimal
    spread_cost: Decimal | None
    slippage_cost: Decimal | None


class EvidenceProvenance(CanonicalModel):
    agent_run_id: NonEmptyStr
    source_artifact_sha256: NonEmptyStr
    dataset_sha256: NonEmptyStr | None
    registered_at: datetime
    occurred_at: datetime
    registration_state: RegistrationState
    holdout_state: HoldoutState


class EvidenceBundle(CanonicalModel):
    bundle_schema_version: Literal[1] = 1
    result_schema_version: PositiveInt
    trial_id: NonEmptyStr
    attempt_id: NonEmptyStr
    spec_sha256: NonEmptyStr
    source_result_sha256: NonEmptyStr
    result: BacktestResult
    daily_returns: tuple[DailyReturnPoint, ...]
    return_series_basis: ReturnSeriesBasis
    costs: CostSummary
    provenance: EvidenceProvenance


class StoredEvidence(CanonicalModel):
    sha256: str
    path: str
    size_bytes: NonNegativeInt
```

Use the same `ensure_utc` field validator for `registered_at` and `occurred_at`. `registered_at` is operator-declared provenance; the database event's `recorded_at` is the only registration-order authority. Validate that `source_result_sha256 == result.digest()` in an `EvidenceBundle` model validator. Reject a `COMPLETE` `CostSummary` with either `spread_cost` or `slippage_cost` set to `None`.

Implement the store with this path and write behavior:

```python
class EvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / f"{digest}.json"

    def write(self, bundle: EvidenceBundle) -> StoredEvidence:
        data = canonical_bytes(bundle)
        digest = hashlib.sha256(data).hexdigest()
        target = self._path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != data:
                raise EvidenceIntegrityError()
            return StoredEvidence(
                sha256=digest,
                path=target.relative_to(self.root).as_posix(),
                size_bytes=len(data),
            )
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".evidence-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != data:
                    raise EvidenceIntegrityError() from None
        finally:
            Path(temporary).unlink(missing_ok=True)
        return StoredEvidence(
            sha256=digest,
            path=target.relative_to(self.root).as_posix(),
            size_bytes=len(data),
        )

    def read(self, digest: str) -> EvidenceBundle:
        target = self._path(digest)
        if not target.is_file():
            raise EvidenceIntegrityError()
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise EvidenceIntegrityError()
        try:
            bundle = EvidenceBundle.model_validate_json(data)
        except (ValueError, TypeError) as error:
            raise EvidenceIntegrityError() from error
        if canonical_bytes(bundle) != data:
            raise EvidenceIntegrityError()
        return bundle

    def verify(self, digest: str) -> None:
        self.read(digest)
```

Use `EvidenceIntegrityError` from this task. Do not swallow `OSError`: wrap it in `EvidenceIntegrityError` from a credential-free private cause.

Extend `src/trading_house/research/__init__.py` again with `DailyReturnPoint`, `CostSummary`, `EvidenceProvenance`, `EvidenceBundle`, `StoredEvidence`, and `EvidenceStore`, keeping `__all__` sorted.

- [ ] **Step 4: Run the Hypothesis invariants**

The property test file was created before implementation in Step 1. It imports `given` from `hypothesis` and `st` from `hypothesis.strategies`. Run its assertions now that the canonical store exists:

```python
@given(st.integers(min_value=0, max_value=32))
def test_canonical_digest_is_stable(day_offset: int) -> None:
    assert canonical_sha256(_bundle(day_offset=day_offset)) == canonical_sha256(
        _bundle(day_offset=day_offset)
    )


@given(st.integers(min_value=0, max_value=32))
def test_bundle_source_digest_matches_its_result(day_offset: int) -> None:
    bundle = _bundle(day_offset=day_offset)
    assert bundle.source_result_sha256 == bundle.result.digest()
```

Also generate two `TrialProtocol` instances from equal mapping payloads with different key insertion order, validate both through `TrialProtocol`, and assert identical canonical bytes.

- [ ] **Step 5: Run focused tests and quality checks**

```powershell
uv run pytest tests/unit/research/test_evidence.py tests/property/test_trial_evidence.py -q --no-cov
uv run ruff format src/trading_house/research/evidence.py tests/unit/research/test_evidence.py tests/property/test_trial_evidence.py
uv run ruff check src/trading_house/research/evidence.py tests/unit/research/test_evidence.py tests/property/test_trial_evidence.py
uv run mypy
```

Expected: all evidence tests pass and no quality check reports an error.

- [ ] **Step 6: Commit the evidence layer**

```powershell
git add src/trading_house/core/errors.py src/trading_house/research/evidence.py src/trading_house/research/__init__.py tests/unit/research/test_evidence.py tests/property/test_trial_evidence.py
git commit -m "feat: add canonical research evidence bundles"
```

---

### Task 3: Create the dedicated PostgreSQL ledger and append-only store

**Files:**
- Create: `migrations/versions/0007_trial_ledger_events.py`
- Create: `src/trading_house/research/ledger_store.py`
- Create: `src/trading_house/ops/ledger.py`
- Modify: `src/trading_house/research/trial_ledger.py`
- Modify: `src/trading_house/settings.py`
- Modify: `src/trading_house/core/errors.py`
- Modify: `tests/unit/test_settings.py`
- Modify: `tests/conftest.py`
- Modify: `docker/postgres/init-roles.sh`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Test: `tests/integration/research/test_trial_ledger_store.py`

**Interfaces:**
- `PostgresTrialLedger(connection_factory)` implements `register(protocol)`, `append(event)`, `events`, `events_for`, `replay`, `counters`, and `verify`.
- `LedgerRecord` and `LedgerIntegrityReport` are defined in `research/trial_ledger.py` in Task 1; the store returns those exact models rather than defining parallel row types.
- `ops/ledger.py` exposes `build_evidence_store(settings)`, `result_recorded_event(bundle)`, and `evidence_sealed_event(bundle, evidence_sha256)`. The CLI owns the migration-head check and constructs `PostgresTrialLedger` after it passes.

- [ ] **Step 1: Write the failing database integration tests**

Add `tests/integration/research/test_trial_ledger_store.py`. The tests use the existing `database` fixture, a local `_protocol()` helper copied from Task 1, and this fixture:

```python
@pytest.fixture
def trial_protocol() -> TrialProtocol:
    return _protocol()
```

Define these event helpers in the same test module. Import `NAMESPACE_URL`, `uuid5`, `UTC`, `datetime`, `canonical_sha256`, `ScopeKind`, `LedgerEventType`, `RegistrationState`, `ExecutionStartedPayload`, `PreregisteredPayload`, and `ResultRecordedPayload`:

```python
def _preregistered_event(protocol: TrialProtocol) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, f"test:protocol:{protocol.protocol_id}"),
        scope_kind=ScopeKind.PROTOCOL,
        scope_id=protocol.protocol_id,
        event_type=LedgerEventType.PREREGISTERED,
        spec_sha256=canonical_sha256(protocol),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=PreregisteredPayload(
            event_type=LedgerEventType.PREREGISTERED,
            protocol=protocol,
            registration_state=RegistrationState.PROSPECTIVE,
        ),
    )


def _execution_started_event(trial_id: str) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, f"test:execution:{trial_id}"),
        scope_kind=ScopeKind.ATTEMPT,
        scope_id=f"attempt-{trial_id}",
        event_type=LedgerEventType.EXECUTION_STARTED,
        trial_id=trial_id,
        attempt_id=f"attempt-{trial_id}",
        spec_sha256="d" * 64,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=ExecutionStartedPayload(
            event_type=LedgerEventType.EXECUTION_STARTED,
            attempt_id=f"attempt-{trial_id}",
            execution_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )


def _result_event(trial_id: str) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, f"test:result:{trial_id}"),
        scope_kind=ScopeKind.ATTEMPT,
        scope_id=f"attempt-{trial_id}",
        event_type=LedgerEventType.RESULT_RECORDED,
        trial_id=trial_id,
        attempt_id=f"attempt-{trial_id}",
        spec_sha256="d" * 64,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=ResultRecordedPayload(
            event_type=LedgerEventType.RESULT_RECORDED,
            attempt_id=f"attempt-{trial_id}",
            source_result_sha256="a" * 64,
        ),
    )
```

Cover:

```python
def test_genesis_and_chained_events_verify(research_ledger_dsn, research_evidence_root, trial_protocol):
    ledger = PostgresTrialLedger(lambda: open_runtime_connection(research_ledger_dsn))
    first = ledger.append(_preregistered_event(trial_protocol))
    second = ledger.append(_execution_started_event(trial_protocol.candidates[0].trial_id))
    report = ledger.verify()

    assert report.valid
    assert report.checked_events == 2
    assert first.sequence == 1
    assert second.previous_hash == first.event_hash


def test_result_before_registration_is_refused(research_ledger_dsn, trial_protocol):
    ledger = PostgresTrialLedger(lambda: open_runtime_connection(research_ledger_dsn))
    with pytest.raises(TrialLedgerAppendError):
        ledger.append(_result_event(trial_protocol.candidates[0].trial_id))


def test_same_event_id_with_different_payload_is_refused(research_ledger_dsn, trial_protocol):
    ledger = PostgresTrialLedger(lambda: open_runtime_connection(research_ledger_dsn))
    event = _preregistered_event(trial_protocol)
    ledger.append(event)
    with pytest.raises(TrialLedgerAppendError):
        ledger.append(event.model_copy(update={"scope_id": "different"}))


def test_rolled_back_sequence_gap_does_not_break_chain(research_ledger_dsn, research_migration_dsn, trial_protocol):
    ledger = PostgresTrialLedger(lambda: open_runtime_connection(research_ledger_dsn))
    ledger.append(_preregistered_event(trial_protocol))
    with psycopg.connect(research_migration_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT nextval('research.trial_ledger_events_sequence_seq')")
        connection.rollback()
    assert ledger.verify().valid
```

Add a `ThreadPoolExecutor(max_workers=8)` test that appends eight events on distinct connections and asserts one continuous chain. Add a mutation test using `research_migration_dsn` (the database owner, which still fires the trigger) that `UPDATE`, `DELETE`, and `TRUNCATE` each raise. Add a test that the runtime role cannot execute direct `INSERT`.

Before implementing `settings.py`, extend `tests/unit/test_settings.py` with the two research environment suffixes and assert that the DSN remains optional while the evidence root defaults to `.local/evidence`. The two DSNs must be two different databases, as they are in the code below — a fixture that pointed both at one DSN would configure exactly the collision `ops/ledger.py` refuses:

```python
SECRET_DSN = "postgresql://runtime:super-secret-password@localhost/trading_house"  # noqa: S105
RESEARCH_DSN = "postgresql://runtime:super-secret-password@localhost/trading_house_research"


def test_research_dsn_is_optional_for_non_trial_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)

    settings = RuntimeSettings()

    assert settings.research_ledger_dsn is None
    assert settings.evidence_root == Path(".local/evidence")


def test_research_dsn_and_evidence_root_are_read_from_the_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)
    monkeypatch.setenv("TRADING_HOUSE_RESEARCH_LEDGER_DSN", RESEARCH_DSN)
    monkeypatch.setenv("TRADING_HOUSE_EVIDENCE_ROOT", "C:/evidence")

    settings = RuntimeSettings()

    assert settings.research_ledger_dsn is not None
    assert settings.research_ledger_dsn.get_secret_value() == RESEARCH_DSN
    assert settings.evidence_root == Path("C:/evidence")
```

This makes the settings contract part of the persistence task rather than an untested prerequisite for `ops/ledger.py`.

- [ ] **Step 2: Add the second database to the test harness**

Modify `tests/conftest.py` with `RESEARCH_DATABASE = "trading_house_research"`. Add an optional `dbname: str | None = None` argument to `_dsn`, defaulting to `container.dbname`, so the same role DSNs can target either database. Extend `DatabaseHarness` with:

```python
research_migration_dsn: str = field(repr=False)
research_runtime_dsn: str = field(repr=False)
research_alembic_config: Config
```

In `_bootstrap_roles`, after creating the existing roles, execute autocommit:

```python
cursor.execute(
    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(RESEARCH_DATABASE))
)
cursor.execute(
    sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO trading_house_owner").format(
        sql.Identifier(RESEARCH_DATABASE)
    )
)
cursor.execute(
    sql.SQL("GRANT CONNECT ON DATABASE {} TO trading_house_runtime").format(
        sql.Identifier(RESEARCH_DATABASE)
    )
)
cursor.execute(
    sql.SQL("ALTER DATABASE {} SET TIME ZONE 'UTC'").format(sql.Identifier(RESEARCH_DATABASE))
)
```

Create `_research_migration_url(container)` using `RESEARCH_DATABASE`, run `command.upgrade` on both Alembic configs, and yield the four DSNs. Add these test-facing fixtures:

```python
@pytest.fixture
def research_ledger_dsn(database: DatabaseHarness) -> str:
    return database.research_runtime_dsn


@pytest.fixture
def research_migration_dsn(database: DatabaseHarness) -> str:
    return database.research_migration_dsn


@pytest.fixture
def research_evidence_root(tmp_path: Path) -> Path:
    return tmp_path / "evidence"
```

Add an `isolated_research_ledger` fixture that downgrades/upgrades the research config around one test. Do not reset the main application database for research tests.

- [ ] **Step 3: Write migration `0007_trial_ledger_events.py`**

Use `down_revision = "0006_position_events"`. `upgrade()` starts with `SET ROLE trading_house_owner` and creates:

```sql
CREATE TABLE research.trial_ledger_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('protocol', 'trial', 'attempt')),
    scope_id TEXT NOT NULL,
    trial_id TEXT,
    attempt_id TEXT,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'preregistered', 'legacy_imported', 'execution_started',
        'result_recorded', 'failed', 'evidence_sealed',
        'validated', 'gate_decided'
    )),
    spec_sha256 BYTEA NOT NULL,
    canonical_event BYTEA NOT NULL,
    event_json JSONB NOT NULL,
    payload_sha256 BYTEA NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    previous_hash BYTEA NOT NULL,
    event_hash BYTEA NOT NULL,
    legacy_import BOOLEAN NOT NULL DEFAULT FALSE,
    legacy_reason TEXT,
    CONSTRAINT ledger_event_previous_hash_size CHECK (pg_catalog.octet_length(previous_hash) = 32),
    CONSTRAINT ledger_event_hash_size CHECK (pg_catalog.octet_length(event_hash) = 32),
    CONSTRAINT ledger_event_spec_hash_size CHECK (pg_catalog.octet_length(spec_sha256) = 32),
    CONSTRAINT ledger_event_payload_hash_size CHECK (pg_catalog.octet_length(payload_sha256) = 32),
    CONSTRAINT ledger_genesis_previous_hash CHECK (
        sequence <> 1 OR previous_hash = pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex')
    )
);
CREATE INDEX ledger_events_trial_idx ON research.trial_ledger_events (trial_id, sequence);
CREATE TABLE research.trial_ledger_heads (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    last_sequence BIGINT NOT NULL DEFAULT 0,
    last_event_hash BYTEA NOT NULL DEFAULT pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex')
);
INSERT INTO research.trial_ledger_heads (singleton) VALUES (TRUE);
```

Add `research.reject_trial_ledger_mutation()` and row/truncate triggers using the exact `RAISE EXCEPTION ... ERRCODE = '55000'` style from `0001_audit_ledger.py`. The append function must be `SECURITY DEFINER`, `SET search_path = pg_catalog`, take `(BYTEA, JSONB, BYTEA)`, acquire `pg_advisory_xact_lock(19573, 1)`, lock the singleton head row `FOR UPDATE`, and verify canonical bytes equal JSONB. Check for an existing event ID before comparing the expected previous hash so a retry of an already committed event is idempotent even when the chain has advanced; return the existing row when its canonical bytes match and raise a conflict otherwise. For a new event, verify `p_expected_previous_hash` equals the head hash; raise SQLSTATE `40001` when it does not. Then compute the next sequence and:

```sql
audit_crypto.digest(
    pg_catalog.convert_to('trading-house:trial-ledger:v1', 'UTF8')
    || pg_catalog.int8send(next_sequence)
    || previous_hash
    || p_canonical_event,
    'sha256'
)
```

insert the event, update the head row, and return the inserted row. Grant only schema usage, table select, and function execute to `trading_house_runtime`; revoke public access to the table, triggers, and function. `downgrade()` drops triggers, function, indexes, tables, and grants without dropping the `research` schema.

- [ ] **Step 4: Implement the PostgreSQL repository and verifier**

Create `src/trading_house/research/ledger_store.py`. Import `NAMESPACE_URL` and `uuid5` from `uuid`. Follow `audit/repository.py` exactly for the connection-factory protocol, `_OperationFailure` sentinel, redacted private cause, and `with connection, connection.cursor()` transaction shape.

`register(protocol)` creates one `PREREGISTERED` event containing the complete protocol and returns `protocol.candidates`; its event ID is `uuid5(NAMESPACE_URL, f"trading-house:trial-protocol:{canonical_sha256(protocol)}")`, so a repeated call with the same canonical protocol and candidate tuple is idempotent. `append(event)` must first enforce that a `RESULT_RECORDED`, `FAILED`, or `EVIDENCE_SEALED` event for a trial has an existing registration or `LEGACY_IMPORTED` lineage, then call:

```python
cursor.execute(
    "SELECT * FROM research.append_trial_ledger_event(%s, %s, %s)",
    (canonical_event, Jsonb(event_json), expected_previous_hash),
)
```

Read the current head first, compute `payload_sha256` from the canonical payload and `spec_sha256` from the event's declared trial-spec digest, and pass the head hash as the expected previous hash. If the function raises SQLSTATE `40001` for a stale head, close the connection, reread the head, and retry the same deterministic event at most three times; do not retry duplicate-event or validation errors. Convert the two hex digests to `bytes.fromhex` only at the database boundary and back to lowercase hex in `LedgerRecord`. Convert any database failure to `TrialLedgerAppendError()`; never include the DSN or raw driver text.

`events()` uses `SET TRANSACTION READ ONLY` and returns rows ordered by `sequence`. `events_for(trial_id)` uses the same read transaction with the indexed trial filter. `replay()` returns the ordered `LedgerEvent` tuple. `counters()` calls `trial_counters(self.replay())`. `verify()` checks every row's event hash, previous-hash link, payload digest, and canonical bytes; it returns `LedgerIntegrityReport(valid=False, reason=...)` rather than raising for a detected integrity problem.

- [ ] **Step 5: Add the research settings and typed errors**

Before the composition shim, add to `src/trading_house/settings.py`:

```python
research_ledger_dsn: SecretStr | None = None
evidence_root: Path = Path(".local/evidence")
```

The research DSN is optional at the application boundary so unrelated commands do not require a second database. `ops/ledger.py` converts its absence into `ConfigurationError` only for a research-ledger operation.

`ExitCode.EVIDENCE_INTEGRITY = 17` and its `cli.EXIT_CODES` mapping already exist from Task 2. Leave them unchanged. Add only the two ledger-specific codes/classes in this task. In `core/errors.py`, add:

```python
class ExitCode(IntEnum):
    OK = 0
    CONFIGURATION = 2
    SIGNATURE = 3
    DATABASE = 4
    MIGRATION = 5
    AUDIT_INTEGRITY = 6
    AUDIT_APPEND = 7
    BROKER = 8
    ACCOUNT_MODE = 9
    COVERAGE = 10
    INSUFFICIENT_HISTORY = 11
    DUPLICATE_INTENT = 12
    UNRESOLVED_INTENTS = 13
    CONCURRENT_SUBMISSION = 14
    TRIAL_LEDGER_APPEND = 15
    TRIAL_LEDGER_INTEGRITY = 16
    EVIDENCE_INTEGRITY = 17


class TrialLedgerAppendError(TradingHouseError):
    public_message = "trial ledger append failed"


class TrialLedgerIntegrityError(TradingHouseError):
    public_message = "trial ledger integrity verification failed"
```

`EvidenceIntegrityError` and exit code 17 were added and mapped in Task 2. Do not re-add either. In `ops/ledger.py`:

```python
def build_evidence_store(settings: RuntimeSettings) -> EvidenceStore:
    return EvidenceStore(settings.evidence_root)
```

- [ ] **Step 6: Provision the dedicated database locally**

Add `TRADING_HOUSE_RESEARCH_DATABASE: trading_house_research` to `compose.yaml`. In `docker/postgres/init-roles.sh`, add this exact block after the existing role grants:

```sh
psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=research_database="$TRADING_HOUSE_RESEARCH_DATABASE" <<'SQL'
SELECT pg_catalog.format('CREATE DATABASE %I', :'research_database')
WHERE NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_database WHERE datname = :'research_database'
)
\gexec
GRANT CONNECT, CREATE ON DATABASE trading_house TO trading_house_owner;
GRANT CONNECT, CREATE ON DATABASE trading_house_research TO trading_house_owner;
GRANT CONNECT ON DATABASE trading_house TO trading_house_runtime;
GRANT CONNECT ON DATABASE trading_house_research TO trading_house_runtime;
ALTER DATABASE trading_house SET TIME ZONE 'UTC';
ALTER DATABASE trading_house_research SET TIME ZONE 'UTC';
SQL
```

Use the literal `trading_house_research` in the grant statements because the local compose value is fixed; the `CREATE DATABASE` line is parameterized so the script remains valid if the variable changes. Add these documented values to `.env.example`:

```dotenv
TRADING_HOUSE_RESEARCH_DATABASE=trading_house_research
TRADING_HOUSE_RESEARCH_LEDGER_DSN=postgresql://trading_house_runtime:development-only-runtime-password@127.0.0.1:5432/trading_house_research
TRADING_HOUSE_EVIDENCE_ROOT=.local/evidence
```

- [ ] **Step 7: Run migration and integration tests**

Start the container and run both databases' migrations:

```powershell
uv run pytest tests/unit/test_settings.py -q --no-cov
docker compose up -d postgres
uv run alembic -x url=postgresql+psycopg://trading_house_migrator:development-only-migrator-password@127.0.0.1:5432/trading_house upgrade head
uv run alembic -x url=postgresql+psycopg://trading_house_migrator:development-only-migrator-password@127.0.0.1:5432/trading_house_research upgrade head
uv run pytest tests/integration/research/test_trial_ledger_store.py -q --no-cov
```

Expected: the migration reaches `0007_trial_ledger_events` in both databases and all ledger integration tests pass.

- [ ] **Step 8: Commit the persistence layer**

```powershell
git add migrations/versions/0007_trial_ledger_events.py src/trading_house/research/ledger_store.py src/trading_house/research/trial_ledger.py src/trading_house/ops/ledger.py src/trading_house/settings.py src/trading_house/core/errors.py tests/conftest.py tests/unit/test_settings.py tests/integration/research/test_trial_ledger_store.py docker/postgres/init-roles.sh compose.yaml .env.example
git commit -m "feat: persist the append-only trial ledger"
```

---

### Task 4: Import Phase 7 results and derive honest legacy returns

**Files:**
- Create: `src/trading_house/research/legacy_import.py`
- Test: `tests/unit/research/test_legacy_import.py`
- Test: `tests/integration/research/test_trial_cli.py` (import path coverage is added in Task 5)

**Interfaces:**
- `derive_realized_daily_returns(result: BacktestResult) -> tuple[DailyReturnPoint, ...]`.
- `import_phase7_artifact(path: Path, *, ledger: PostgresTrialLedger, evidence: EvidenceStore, now: datetime) -> LegacyImportResult`.
- `LegacyImportResult` is a frozen model with `trial_id: NonEmptyStr`, `attempt_id: NonEmptyStr`, `source_result_sha256: NonEmptyStr`, `evidence_sha256: NonEmptyStr`, and `already_present: bool`.

- [ ] **Step 1: Write failing derivation and import tests**

Create `tests/unit/research/test_legacy_import.py` with a small `BacktestResult` fixture containing two trades on different UTC days. Set `_NOW = datetime(2024, 1, 1, tzinfo=UTC)`. Copy the `_cost_model`, `_trade`, and `_result` helpers verbatim from `tests/unit/research/backtest/test_result.py:18-79`, then add this concrete two-day builder:

```python
def _result_with_two_trades() -> BacktestResult:
    first = _trade(
        entry_at=_NOW,
        exit_at=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
        gross_pnl=Decimal("1000"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("1000"),
    )
    second = _trade(
        entry_at=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
        exit_at=datetime(2024, 1, 3, 12, 0, tzinfo=UTC),
        gross_pnl=Decimal("0"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("0"),
    )
    return _result(
        start=_NOW,
        end=datetime(2024, 1, 3, 23, 59, tzinfo=UTC),
        trades=(first, second),
        net_pnl=Decimal("1000"),
    )
```

Add this exact outer-artifact helper:

```python
def _write_outer_artifact(path: Path, result: BacktestResult) -> Path:
    artifact = path / "phase7.json"
    artifact.write_text(
        json.dumps(
            {
                "status": "ok",
                "result": json.loads(result.model_dump_json()),
                "digest": result.digest(),
                "margin_modelled": False,
            }
        ),
        encoding="utf-8",
    )
    return artifact
```

```python
class FakeLedger:
    def __init__(self) -> None:
        self.events: list[LedgerEvent] = []

    @property
    def append_count(self) -> int:
        return len(self.events)

    def append(self, event: LedgerEvent) -> None:
        self.events.append(event)

    def events_for(self, trial_id: str) -> tuple[LedgerEvent, ...]:
        return tuple(event for event in self.events if event.trial_id == trial_id)


def _fake_ledger() -> FakeLedger:
    return FakeLedger()
```

Pass `_fake_ledger()` to the importer with `typing.cast(PostgresTrialLedger, fake)`; the importer's production dependency remains the concrete PostgreSQL ledger. Assert:

```python
def test_realized_returns_include_every_utc_calendar_day() -> None:
    points = derive_realized_daily_returns(_result_with_two_trades())

    assert [point.day.isoformat() for point in points] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
    ]
    assert points[1].value == Decimal("0.01")
    assert points[2].value == Decimal("0")


def test_import_verifies_the_original_result_digest(tmp_path: Path) -> None:
    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["digest"] = "0" * 64
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceIntegrityError):
        import_phase7_artifact(
            artifact,
            ledger=cast(PostgresTrialLedger, _fake_ledger()),
            evidence=EvidenceStore(tmp_path / "evidence"),
            now=_NOW,
        )


def test_legacy_import_is_idempotent_by_source_digest(tmp_path: Path) -> None:
    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    ledger = _fake_ledger()

    first = import_phase7_artifact(
        artifact, ledger=cast(PostgresTrialLedger, ledger), evidence=EvidenceStore(tmp_path / "evidence"), now=_NOW
    )
    second = import_phase7_artifact(
        artifact, ledger=cast(PostgresTrialLedger, ledger), evidence=EvidenceStore(tmp_path / "evidence"), now=_NOW
    )

    assert second.already_present
    assert ledger.append_count == 1
```

Add assertions that the legacy bundle has `provenance.registration_state=LEGACY_UNPREREGISTERED`, `provenance.holdout_state=CONTAMINATED`, `provenance.dataset_sha256=None`, `return_series_basis=REALIZED_CLOSED_TRADES`, and `costs.status=PARTIAL` with unknown spread/slippage.

- [ ] **Step 2: Run tests and verify failure**

```powershell
uv run pytest tests/unit/research/test_legacy_import.py -q --no-cov
```

Expected: import failure because `research.legacy_import` does not exist.

- [ ] **Step 3: Implement daily-return derivation**

Create `src/trading_house/research/legacy_import.py` with:

```python
def derive_realized_daily_returns(result: BacktestResult) -> tuple[DailyReturnPoint, ...]:
    by_day: dict[date, Decimal] = {}
    for trade in result.trades:
        by_day[trade.exit_at.date()] = by_day.get(trade.exit_at.date(), Decimal(0)) + trade.net_pnl

    equity = result.firm_equity
    previous = equity
    points: list[DailyReturnPoint] = []
    day = result.start.date()
    while day <= result.end.date():
        equity += by_day.get(day, Decimal(0))
        if previous <= 0:
            raise EvidenceIntegrityError()
        points.append(DailyReturnPoint(day=day, value=(equity - previous) / previous))
        previous = equity
        day += timedelta(days=1)
    return tuple(points)
```

The function deliberately produces realized closed-trade returns only. It does not claim to be a mark-to-market series and does not infer spread/slippage costs.

- [ ] **Step 4: Implement the idempotent importer**

The importer must:

1. read the outer JSON;
2. validate `result` with `BacktestResult.model_validate_json(json.dumps(payload["result"]))`;
3. refuse when `payload["digest"] != result.digest()`;
4. compute `source_artifact_sha256` from the file bytes;
5. derive `trial_id = f"legacy-{result.digest()[:16]}"` and a deterministic UUID5 event ID;
6. build a `TrialSpec` with `spec_id="session-momentum-legacy"`, rationale `"Phase 7 result imported after the concrete ledger existed"`, and an empty-but-valid parameter tuple;
7. build a `CostSummary(status=PARTIAL, commission=sum(commission), swap=sum(swap), spread_cost=None, slippage_cost=None)` and set `EvidenceProvenance.registration_state=LEGACY_UNPREREGISTERED`, `EvidenceProvenance.holdout_state=CONTAMINATED`, and `EvidenceProvenance.dataset_sha256=None`;
8. build the `EvidenceBundle` with `source_result_sha256=result.digest()` and the derived returns;
9. write the bundle before appending the event;
10. append `LedgerEventType.LEGACY_IMPORTED` with `scope_kind=TRIAL`, `trial_id`, `attempt_id`, `spec_sha256`, and the evidence/source digests;
11. return `already_present=True` when the deterministic event already exists.

The importer must not create a `PREREGISTERED` event and must not invent a dataset hash.

- [ ] **Step 5: Run focused tests and quality checks**

```powershell
uv run pytest tests/unit/research/test_legacy_import.py -q --no-cov
uv run ruff format src/trading_house/research/legacy_import.py tests/unit/research/test_legacy_import.py
uv run ruff check src/trading_house/research/legacy_import.py tests/unit/research/test_legacy_import.py
uv run mypy
```

Expected: all legacy tests pass and quality checks are clean.

- [ ] **Step 6: Commit legacy import**

```powershell
git add src/trading_house/research/legacy_import.py tests/unit/research/test_legacy_import.py
git commit -m "feat: import phase 7 results as legacy evidence"
```

---

### Task 5: Expose the six research trial commands

**Files:**
- Modify: `src/trading_house/cli.py`
- Modify: `src/trading_house/research/trial_ledger.py`
- Modify: `src/trading_house/research/__init__.py`
- Modify: `tests/unit/test_cli.py`
- Create: `tests/integration/research/test_trial_cli.py`

**Interfaces:**
- `RuntimeSettings.research_ledger_dsn: SecretStr | None = None`.
- `RuntimeSettings.evidence_root: Path = Path(".local/evidence")`.
- `TrialLedger` protocol becomes `register(protocol)`, `append(event)`, `events_for(trial_id)`, `counters()`, and `verify()`.
- CLI group path is `trading-house research trial {register,record,import-legacy,show,count,verify}`.

- [ ] **Step 1: Write failing CLI tests**

Extend the command-group tuple in `tests/unit/test_cli.py` to include `"research"`. Add unit tests for `TrialLedgerAppendError` and `TrialLedgerIntegrityError` in `EXIT_CODES`, using the existing `_raiser` helper and stable exit codes 15 and 16. `EvidenceIntegrityError`/17 is already mapped by Task 2; do not duplicate it. The settings contract is already covered by Task 3; this task only verifies that the CLI consumes those fields.

- [ ] **Step 2: Run the tests and verify failure**

```powershell
uv run pytest tests/unit/test_cli.py -q --no-cov
```

Expected: failures for the missing command group and error mappings.

- [ ] **Step 3: Evolve the ledger protocol**

The settings fields were added in Task 3 so the composition shim can type-check before the CLI is written. Replace the two-method `TrialLedger` protocol with:

```python
class TrialLedger(Protocol):
    def register(self, protocol: TrialProtocol) -> Sequence[TrialSpec]: ...
    def append(self, event: LedgerEvent) -> LedgerRecord: ...
    def events_for(self, trial_id: str) -> Sequence[LedgerEvent]: ...
    def counters(self) -> TrialCounters: ...
    def verify(self) -> LedgerIntegrityReport: ...
```

Keep the old `Trial` and `deflation_trial_count` functions exported for compatibility.

- [ ] **Step 4: Add the CLI group and commands**

In `cli.py`, import `EvidenceStore`, `PostgresTrialLedger`, `build_evidence_store`, `result_recorded_event`, `evidence_sealed_event`, the JSON model loaders, and `import_phase7_artifact`. Add `research_app` and `trial_app`, register them as:

```python
research_app = typer.Typer(no_args_is_help=True, help="Research commands.")
trial_app = typer.Typer(no_args_is_help=True, help="Trial-ledger commands.")
app.add_typer(research_app, name="research")
research_app.add_typer(trial_app, name="trial")
```

Add `_research_settings()` and `_load_json_model(path, model)` helpers. `_research_settings()` calls `_settings()`; a missing research DSN raises `ConfigurationError`. `_load_json_model` reads UTF-8 and calls the model's `model_validate_json`. Add this composition helper so every research-ledger command fails with `MigrationMismatchError` before touching the schema:

```python
def _trial_ledger() -> PostgresTrialLedger:
    settings = _settings()
    if settings.research_ledger_dsn is None:
        raise ConfigurationError()
    connection = open_runtime_connection(settings.research_ledger_dsn)
    try:
        assert_at_head(connection, Config(str(DEFAULT_ALEMBIC_CONFIG)))
    finally:
        connection.close()
    return PostgresTrialLedger(lambda: open_runtime_connection(settings.research_ledger_dsn))
```

`_evidence_store()` calls `build_evidence_store(_settings())`.

Add these event builders to `src/trading_house/ops/ledger.py`, so the CLI does not invent event payloads. Import `NAMESPACE_URL` and `uuid5` from `uuid`; both event IDs are deterministic so a retried `record` command is idempotent:

```python
def result_recorded_event(bundle: EvidenceBundle) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid5(
            NAMESPACE_URL,
            f"trading-house:trial-event:result:{bundle.attempt_id}:{bundle.source_result_sha256}",
        ),
        scope_kind=ScopeKind.ATTEMPT,
        scope_id=bundle.attempt_id,
        event_type=LedgerEventType.RESULT_RECORDED,
        trial_id=bundle.trial_id,
        attempt_id=bundle.attempt_id,
        spec_sha256=bundle.spec_sha256,
        occurred_at=bundle.provenance.occurred_at,
        payload=ResultRecordedPayload(
            event_type=LedgerEventType.RESULT_RECORDED,
            attempt_id=bundle.attempt_id,
            source_result_sha256=bundle.source_result_sha256,
        ),
    )


def evidence_sealed_event(bundle: EvidenceBundle, evidence_sha256: str) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid5(
            NAMESPACE_URL,
            f"trading-house:trial-event:seal:{bundle.attempt_id}:{evidence_sha256}",
        ),
        scope_kind=ScopeKind.ATTEMPT,
        scope_id=bundle.attempt_id,
        event_type=LedgerEventType.EVIDENCE_SEALED,
        trial_id=bundle.trial_id,
        attempt_id=bundle.attempt_id,
        spec_sha256=bundle.spec_sha256,
        occurred_at=bundle.provenance.occurred_at,
        payload=EvidenceSealedPayload(
            event_type=LedgerEventType.EVIDENCE_SEALED,
            attempt_id=bundle.attempt_id,
            evidence_sha256=evidence_sha256,
        ),
    )
```

Import those two functions in `cli.py` and call them without an extra `attempt_id` argument; the bundle is the single source of identity.

Implement these exact command signatures and payloads:

```python
@trial_app.command("register")
def research_trial_register(
    protocol: Annotated[Path, typer.Option("--protocol", help="Frozen TrialProtocol JSON.")],
) -> None:
    def operation() -> dict[str, JsonValue]:
        parsed = _load_json_model(protocol, TrialProtocol)
        trials = _trial_ledger().register(parsed)
        return {
            "protocol_id": parsed.protocol_id,
            "trial_count": len(trials),
            "trial_ids": [trial.trial_id for trial in trials],
        }
    _run(operation)


@trial_app.command("record")
def research_trial_record(
    trial_id: Annotated[str, typer.Option("--trial-id")],
    attempt_id: Annotated[str, typer.Option("--attempt-id")],
    evidence: Annotated[Path, typer.Option("--evidence", help="Evidence bundle JSON.")],
) -> None:
    def operation() -> dict[str, JsonValue]:
        bundle = _load_json_model(evidence, EvidenceBundle)
        if bundle.trial_id != trial_id or bundle.attempt_id != attempt_id:
            raise SchemaValidationError()
        stored = _evidence_store().write(bundle)
        ledger = _trial_ledger()
        ledger.append(result_recorded_event(bundle))
        ledger.append(evidence_sealed_event(bundle, stored.sha256))
        return {"trial_id": trial_id, "evidence_sha256": stored.sha256}
    _run(operation)


@trial_app.command("import-legacy")
def research_trial_import_legacy(
    artifact: Annotated[Path, typer.Option("--artifact", help="Phase 7 outer result JSON.")],
) -> None:
    def operation() -> dict[str, JsonValue]:
        result = import_phase7_artifact(
            artifact,
            ledger=_trial_ledger(),
            evidence=_evidence_store(),
            now=datetime.now(UTC),
        )
        return {
            "trial_id": result.trial_id,
            "source_result_sha256": result.source_result_sha256,
            "evidence_sha256": result.evidence_sha256,
            "already_present": result.already_present,
        }
    _run(operation)


@trial_app.command("show")
def research_trial_show(
    trial_id: Annotated[str, typer.Option("--trial-id")],
) -> None:
    def operation() -> dict[str, JsonValue]:
        events = _trial_ledger().events_for(trial_id)
        return {"trial_id": trial_id, "events": [_event_payload(event) for event in events]}
    _run(operation)


@trial_app.command("count")
def research_trial_count() -> None:
    def operation() -> dict[str, JsonValue]:
        counters = _trial_ledger().counters()
        return {
            "audit_attempts": counters.audit_attempts,
            "selection_lotteries": counters.selection_lotteries,
            "effective_specifications": counters.effective_specifications,
        }
    _run(operation)


@trial_app.command("verify")
def research_trial_verify() -> None:
    def operation() -> dict[str, JsonValue]:
        ledger = _trial_ledger()
        report = ledger.verify()
        if not report.valid:
            return {"valid": False, "checked_events": report.checked_events, "reason": report.reason}
        evidence = _evidence_store()
        for event in ledger.replay():
            if isinstance(event.payload, (EvidenceSealedPayload, LegacyImportedPayload)):
                evidence.verify(event.payload.evidence_sha256)
        return {"valid": True, "checked_events": report.checked_events, "reason": None}
    payload = _execute(operation)
    _emit(payload, status="ok" if payload["valid"] else "invalid")
    if not payload["valid"]:
        raise typer.Exit(code=int(ExitCode.TRIAL_LEDGER_INTEGRITY))
```

`_event_payload` returns `event.model_dump(mode="json")` and never returns a raw connection or DSN. `EvidenceIntegrityError`/17 is already mapped; add only the two ledger error mappings if they are not already present.

- [ ] **Step 5: Add CLI integration tests**

Create `tests/integration/research/test_trial_cli.py`. Use `CliRunner`, set `TRADING_HOUSE_RESEARCH_LEDGER_DSN` and `TRADING_HOUSE_EVIDENCE_ROOT` from the second database fixture and `TRADING_HOUSE_DATABASE_DSN` from the *first* one, and cover:

- `register` returns the declared candidate count;
- `record` writes and seals a bundle;
- `import-legacy` on a small fixture returns `already_present=false` then `true` on the second call;
- `show` replays only the requested trial;
- `count` returns the three named counters;
- `verify` reports `valid=true` on a clean chain;
- an altered evidence file makes `verify` return `valid=false` or the typed evidence error;
- a missing research DSN exits with `ExitCode.CONFIGURATION`;
- a research DSN naming the application database also exits with `ExitCode.CONFIGURATION`, before anything is written;
- direct runtime `INSERT` is refused.

The two DSNs must name two different databases. Pointing both at the research
database is a test that passes with the two-database boundary switched off: a
trial command cannot tell the difference once the connection is open.

- [ ] **Step 6: Run CLI tests**

```powershell
uv run pytest tests/unit/test_cli.py -q --no-cov
uv run pytest tests/integration/research/test_trial_cli.py -q --no-cov
uv run ruff format src/trading_house/cli.py tests/unit/test_cli.py tests/integration/research/test_trial_cli.py
uv run ruff check src/trading_house/cli.py tests/unit/test_cli.py tests/integration/research/test_trial_cli.py
uv run mypy
```

Expected: all focused tests pass and quality checks are clean.

- [ ] **Step 7: Commit the CLI layer**

```powershell
git add src/trading_house/cli.py src/trading_house/research/trial_ledger.py src/trading_house/research/__init__.py tests/unit/test_cli.py tests/integration/research/test_trial_cli.py
git commit -m "feat: expose the research trial ledger commands"
```

---

### Task 6: Close schema, architecture, acceptance, and operator documentation gaps

**Files:**
- Modify: `tests/property/test_schema_boundaries.py`
- Modify: `tests/acceptance/test_architecture.py`
- Create: `tests/acceptance/test_phase8a.py`
- Modify: `README.md`
- Modify: `.env.example` if the Task 3 documentation diff needs a final ordering adjustment

**Interfaces:**
- Produces a passing architecture guard for all `trading_house.research` modules.
- Produces a machine-readable 8A acceptance check that does not require the developer's temporary Phase 7 files.
- Documents the exact local database and CLI setup.

- [ ] **Step 1: Add builders for every new datetime-bearing model**

In `tests/property/test_schema_boundaries.py`, Task 1 already registered the five ledger datetime models and Task 2 already registered `EvidenceProvenance`. Do not duplicate those entries. Add builders only for datetime-bearing models introduced by Tasks 3–5, using `AWARE` and valid nested strict models; after those tasks, the property suite must report no missing builder.

Run:

```powershell
uv run pytest tests/property/test_schema_boundaries.py -q --no-cov
```

Expected: all strict/frozen/extra/UTC property cases pass, with no missing builder for any model introduced by Tasks 1–5.

- [ ] **Step 2: Add the research import guard and its guard-the-guard tests**

In `tests/acceptance/test_architecture.py`, add `RESEARCH_ROOT = SOURCE_ROOT / "research"` and an allowlist containing exactly:

```python
RESEARCH_ALLOWED = {
    "trading_house.core",
    "trading_house.database",
    "trading_house.features",
    "trading_house.marketdata",
    "trading_house.research",
    "trading_house.risk",
    "trading_house.strategies",
}
```

Reject imports from `trading_house.brokers`, `trading_house.execution`, `trading_house.agents`, and any other top-level package. Add a negative guard case that fails on `from trading_house.brokers.mt5.gateway import Mt5Gateway` and a positive case that permits `from trading_house.research.backtest.result import BacktestResult` and `from trading_house.database.connection import open_runtime_connection`.

Do not widen `BACKTEST_ALLOWED`; the new files must live at `research/` and keep the existing backtest boundary strict.

- [ ] **Step 3: Add 8A acceptance checks**

Create `tests/acceptance/test_phase8a.py` with checks that:

- `research` appears in the root CLI help;
- the six trial commands appear in `research trial --help`;
- `TrialProtocol` rejects an empty candidate family;
- `EvidenceStore` writes and verifies a bundle;
- the ledger migration is at Alembic head in both databases;
- a synthetic protocol registers, records, counts, and verifies through the concrete store;
- the three pinned Phase 7 digests are present as exact constants:

```python
PHASE7_RESULT_DIGESTS = {
    "none": "a10b0fa43fc4ddb0185b3a376caab7c9369ec49e3e347d13171d31ab989f022b",
    "fixed_target": "fe5efadef7f5ea2448957e0bec507f2133755df676ade140b05a120cfb5fbc6b",
    "chandelier": "69867de09ef3993e5aabca78b225b21bb1ef82a4aa4fbcd63a9fd52dfe2e0c54",
}
```

The test asserts the mapping has exactly those three values and never recomputes them from a mutable fixture;
- the architecture research guard's positive and negative cases both execute.

Do not read `C:\\Users\\sourc\\AppData\\Local\\Temp\\opencode` from the test suite. The real three-file import is an operator verification step in Task 7.

- [ ] **Step 4: Update the README**

Add a `## Phase 8A — Canonical evidence and trial ledger` section after the Phase 7 evidence section. Include:

- the two database names and their roles;
- the `TRADING_HOUSE_RESEARCH_LEDGER_DSN` and `TRADING_HOUSE_EVIDENCE_ROOT` variables;
- the six commands with one example invocation each;
- the append-only and fail-closed rules;
- the three legacy Phase 7 digests and their `LEGACY_UNPREGISTERED` status;
- the statement that Phase 8A imports evidence but does not implement WFA, DSR, PBO, CPCV, or promotion.

Extend the operator command table and the typed-error recovery table with the three new exit codes.

- [ ] **Step 5: Run acceptance, property, and architecture checks**

```powershell
uv run pytest tests/property/test_schema_boundaries.py tests/acceptance/test_architecture.py tests/acceptance/test_phase8a.py -q --no-cov
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

Expected: all tests pass, Ruff is clean, and mypy is clean.

- [ ] **Step 6: Commit the guards and documentation**

```powershell
git add tests/property/test_schema_boundaries.py tests/acceptance/test_architecture.py tests/acceptance/test_phase8a.py README.md .env.example
git commit -m "test: close the phase 8a acceptance and architecture gates"
```

---

### Task 7: Run the full verification and the real three-artifact migration

**Files:**
- No source files are created.
- Do not add the temporary Phase 7 JSON artifacts, local PostgreSQL data, or `.local/evidence` contents to Git.

**Interfaces:**
- Consumes the completed 8A implementation and the preserved artifacts at `C:\Users\sourc\AppData\Local\Temp\opencode\phase7-none.json`, `phase7-fixed-target.json`, and `phase7-chandelier.json`.
- Produces a verified local research ledger with three legacy trials and an updated operator-facing README if the recorded output differs from the documented values.

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

Expected: all non-integration tests pass, all quality gates pass, the lock is current, and the diff has no whitespace errors.

- [ ] **Step 2: Run the complete container-backed suite**

```powershell
$env:UV_SYSTEM_CERTS = "1"
uv run pytest
```

Expected: the repository suite passes with coverage at or above the configured 95% floor. If Docker is unavailable, stop and report that the integration gate was not run; do not claim 8A complete.

- [ ] **Step 3: Provision the local research database**

```powershell
docker compose up -d postgres
docker compose exec -T postgres psql -U postgres -d trading_house -c "SELECT 1 FROM pg_database WHERE datname = 'trading_house_research'"
```

If the query returns no row, create the database and grants explicitly:

```powershell
docker compose exec -T postgres psql -U postgres -d trading_house -c "CREATE DATABASE trading_house_research"
docker compose exec -T postgres psql -U postgres -d trading_house -c "GRANT CONNECT, CREATE ON DATABASE trading_house_research TO trading_house_owner; GRANT CONNECT ON DATABASE trading_house_research TO trading_house_runtime; ALTER DATABASE trading_house_research SET TIME ZONE 'UTC'"
```

Then run the migrations:

```powershell
uv run alembic -x url=postgresql+psycopg://trading_house_migrator:development-only-migrator-password@127.0.0.1:5432/trading_house upgrade head
uv run alembic -x url=postgresql+psycopg://trading_house_migrator:development-only-migrator-password@127.0.0.1:5432/trading_house_research upgrade head
```

Expected: both commands report Alembic head `0007_trial_ledger_events`.

- [ ] **Step 4: Import the three preserved Phase 7 artifacts**

Set the exact local values and run the importer once per file:

```powershell
$env:TRADING_HOUSE_DATABASE_DSN = "postgresql://trading_house_runtime:development-only-runtime-password@127.0.0.1:5432/trading_house"
$env:TRADING_HOUSE_RESEARCH_LEDGER_DSN = "postgresql://trading_house_runtime:development-only-runtime-password@127.0.0.1:5432/trading_house_research"
$env:TRADING_HOUSE_EVIDENCE_ROOT = ".local/evidence"

uv run trading-house research trial import-legacy --artifact "C:\Users\sourc\AppData\Local\Temp\opencode\phase7-none.json"
uv run trading-house research trial import-legacy --artifact "C:\Users\sourc\AppData\Local\Temp\opencode\phase7-fixed-target.json"
uv run trading-house research trial import-legacy --artifact "C:\Users\sourc\AppData\Local\Temp\opencode\phase7-chandelier.json"
```

Expected: each command exits 0 and reports a distinct `trial_id`, the pinned source digest, and `already_present: false`. The importer derives realized returns and marks every bundle legacy, contaminated, partial-cost, and non-promotable.

- [ ] **Step 5: Verify idempotency, counters, and chain integrity**

```powershell
uv run trading-house research trial import-legacy --artifact "C:\Users\sourc\AppData\Local\Temp\opencode\phase7-none.json"
uv run trading-house research trial count
uv run trading-house research trial verify
```

Expected: the repeated import reports `already_present: true`; `count` reports `audit_attempts=3`, `selection_lotteries=3`, and `effective_specifications=3`; `verify` reports `valid=true` and `checked_events=3`.

- [ ] **Step 6: Check the final repository state**

```powershell
git status --short
git diff --stat
git diff --check
```

Expected: only intended source, test, migration, configuration, and README files are present. No `phase7-*.json`, PostgreSQL dump, DSN, or `.local/evidence` file is staged.

- [ ] **Step 7: Commit only if verification changed a tracked file**

```powershell
git add README.md .env.example
git commit -m "docs: record the phase 8a legacy ledger migration"
```

If neither file changed, do not create an empty commit.

---

## Plan Self-Review Checklist

- [x] Every Phase 8A design section has a task: protocol and events (Task 1), bundles and store (Task 2), PostgreSQL chain (Task 3), legacy import (Task 4), CLI/settings (Task 5), guards/docs (Task 6), verification/migration (Task 7).
- [x] No task adds NumPy or changes the backtest result contract; those belong to 8B/8C.
- [x] The three Phase 7 digests are preserved as pinned values and are never regenerated from mutable test data.
- [x] `LEGACY_IMPORTED` is never represented as preregistration.
- [x] Runtime privileges never include direct event table mutation.
- [x] Sequence gaps from rolled-back transactions are tested as allowed; broken hash links are tested as failures.
- [x] Every new datetime-bearing model is registered in `BUILDERS`.
- [x] Every new CLI command has a unit or integration failure-path test.
- [x] The plan names exact files, symbols, commands, expected results, and commit boundaries.
- [x] No unresolved instruction or reference to an undefined type remains.

---

## Amendment — 2026-09-27: a seventh command, `research trial start`

Added after the plan's own review, and after a review of the plan's result found
the defect this closes. The plan mandated six `research trial` commands and none
of them started an execution, so nothing in `src/` ever constructed an
`ExecutionStartedPayload` — while `trial_counters` counts the deflation
denominators from `EXECUTION_STARTED` and `LEGACY_IMPORTED` alone. An operator
who followed this plan's README verbatim got `audit_attempts=0,
selection_lotteries=0, effective_specifications=0` reported as a fact about
their own record, after a complete `register` + `record`. The plan was wrong
about its own operator surface, not merely incomplete.

So the command set is seven, and `start` is the seventh: `--trial-id`,
`--attempt-id` and a client-asserted `--spec-sha256`, appending one
`EXECUTION_STARTED` event whose id is `uuid5(NAMESPACE_URL,
f"trading-house:trial-event:start:{trial_id}:{attempt_id}")`. Two consequences
are deliberate and are recorded here rather than left for a later reader to
rediscover:

- **`EXECUTION_STARTED` joined the fail-closed set** (`_OUTCOME_EVENT_TYPES`,
  renamed `_REQUIRES_REGISTRATION` in `ledger_store.py`). The plan guarded
  outcomes; a start is a draw from the search space whether or not it is ever
  recorded, so admitting one against an undeclared trial would let anyone
  inflate the denominator the whole phase exists to keep honest.
- **`--spec-sha256` is operator-supplied, not resolved from the
  preregistration.** A protocol seals its whole candidate family inside one
  event, so there is no per-candidate digest in the chain to check against, and
  `ExecutionStartedPayload` carries none. The chain preserves the digest; it
  cannot vouch for it. The README now says so under "What Phase 8A does not
  implement".

One known gap the review surfaced and this amendment does **not** close: because
a start's timestamp is the operator's clock read rather than a time recovered
from a document, a retried `start` of the same attempt id carries the same event
id but different canonical bytes, and the chain's retry-by-id path returns an
existing row only for bytes it already holds. The retry is therefore refused
with exit 15 rather than recognised. The safety property holds — one event, a
chain that still verifies, a denominator that cannot be widened by retrying — but
the exit code is the wrong answer for a genuine retry. Closing it needs either an
operator-supplied `--started-at` on the command (as `import-legacy` has) or a
short-circuit in the store, and both are Phase 8A decisions rather than
housekeeping. The plan's task text above is left exactly as written.

