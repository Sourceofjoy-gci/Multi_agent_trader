"""Every trial the foundry ran, including the ones it did not like."""

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, JsonValue, NonNegativeInt, PositiveInt, field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.values import (
    CanonicalModel,
    FiniteFloat,
    InstrumentId,
    NonEmptyStr,
    PositiveDecimal,
)
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel


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
        parts = (self.start, self.end, self.dataset_sha256)
        any_set = any(part is not None for part in parts)
        all_set = all(part is not None for part in parts)
        if any_set and not all_set:
            raise ValueError("a holdout carries its dates and hash together or not at all")
        if self.state is HoldoutState.NOT_DEFINED and any_set:
            raise ValueError("an undefined holdout cannot carry dates or a hash")
        if (
            self.state in {HoldoutState.LOCKED, HoldoutState.OPENED, HoldoutState.CONSUMED}
            and not all_set
        ):
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
        trial_ids = [candidate.trial_id for candidate in self.candidates]
        spec_ids = [candidate.spec_id for candidate in self.candidates]
        if len(set(trial_ids)) != len(trial_ids) or len(set(spec_ids)) != len(spec_ids):
            raise ValueError("candidate trial ids and spec ids must be unique")
        return self


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

    @field_validator("execution_started_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


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
    report_sha256: NonEmptyStr


class GateDecidedPayload(CanonicalModel):
    event_type: Literal[LedgerEventType.GATE_DECIDED]
    decision: Literal["REJECTED", "RESEARCH_PASSED", "PAPER_APPROVED"]
    report_sha256: NonEmptyStr


LedgerEventPayload = Annotated[
    PreregisteredPayload
    | LegacyImportedPayload
    | ExecutionStartedPayload
    | ResultRecordedPayload
    | FailedPayload
    | EvidenceSealedPayload
    | ValidatedPayload
    | GateDecidedPayload,
    Field(discriminator="event_type"),
]


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
        if event.attempt_id is not None and event.event_type in {
            LedgerEventType.EXECUTION_STARTED,
            LedgerEventType.LEGACY_IMPORTED,
        }:
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


class TrialLedger(Protocol):
    """The five things any trial-ledger consumer may ask of a ledger.

    Read and append, and nothing that hands out a connection or a DSN. The
    operations that need both -- counting the deflation denominators, reading the
    chain back to re-check the evidence it names -- are methods here rather than
    something a caller reaches past the ledger to do, because each of them is
    one read that has to agree with the chain.

    Declared after the models it names, so every annotation below is a real
    reference rather than a string a type checker has to be told to trust.
    """

    def register(self, protocol: TrialProtocol) -> Sequence[TrialSpec]: ...
    def append(self, event: LedgerEvent) -> LedgerRecord: ...
    def events_for(self, trial_id: str) -> Sequence[LedgerRecord]: ...
    def counters(self) -> TrialCounters: ...
    def verify(self) -> LedgerIntegrityReport: ...
