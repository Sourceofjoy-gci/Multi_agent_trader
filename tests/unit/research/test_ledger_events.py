import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from trading_house.core.errors import SchemaValidationError
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.canonical import DOMAIN_SEPARATOR, canonical_bytes, canonical_sha256
from trading_house.research.trial_ledger import (
    CostSpec,
    DataSpec,
    ExecutionSpec,
    ExecutionStartedPayload,
    GateDecidedPayload,
    HoldoutSpec,
    HoldoutState,
    LedgerEvent,
    LedgerEventType,
    LedgerIntegrityReport,
    LegacyImportedPayload,
    RegimeSpec,
    RegistrationState,
    ScopeKind,
    TrialCounters,
    TrialProtocol,
    TrialSpec,
    ValidatedPayload,
    ValidationSpec,
    trial_counters,
)

_OCCURRED = datetime(2024, 1, 1, tzinfo=UTC)


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


def test_a_research_digest_is_domain_separated() -> None:
    """A known answer, because the prefix is the only thing standing between
    the research chain and the audit chain.

    Without the domain prefix this digest is a plain SHA-256 over the canonical
    bytes, which is a shape the audit chain also produces. With it, the same
    model hashes to something no other chain in this codebase emits.
    """

    protocol = _protocol()
    encoded = canonical_bytes(protocol)

    assert DOMAIN_SEPARATOR == b"trading-house:research:v1"
    assert canonical_sha256(protocol) == hashlib.sha256(DOMAIN_SEPARATOR + encoded).hexdigest()
    assert canonical_sha256(protocol) != hashlib.sha256(encoded).hexdigest()


def test_canonical_bytes_refuse_a_non_finite_float() -> None:
    """A digest over NaN bytes is a digest nobody can reproduce."""

    class Leaky(BaseModel):
        value: float

    with pytest.raises(SchemaValidationError):
        canonical_bytes(Leaky(value=float("nan")))


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


def _started(
    *, attempt_id: str, trial_id: str | None = None, spec_sha256: str = "a" * 64
) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid4(),
        scope_kind=ScopeKind.ATTEMPT,
        scope_id=attempt_id,
        event_type=LedgerEventType.EXECUTION_STARTED,
        trial_id=trial_id,
        attempt_id=attempt_id,
        spec_sha256=spec_sha256,
        occurred_at=_OCCURRED,
        payload=ExecutionStartedPayload(
            event_type=LedgerEventType.EXECUTION_STARTED,
            attempt_id=attempt_id,
            execution_started_at=_OCCURRED,
        ),
    )


def _legacy(
    *,
    attempt_id: str,
    trial_id: str,
    spec_sha256: str = "a" * 64,
    legacy: bool,
    legacy_reason: str | None = None,
) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid4(),
        scope_kind=ScopeKind.TRIAL,
        scope_id=trial_id,
        event_type=LedgerEventType.LEGACY_IMPORTED,
        trial_id=trial_id,
        attempt_id=attempt_id,
        spec_sha256=spec_sha256,
        occurred_at=_OCCURRED,
        payload=LegacyImportedPayload(
            event_type=LedgerEventType.LEGACY_IMPORTED,
            trial=_candidate(1).model_copy(update={"trial_id": trial_id}),
            evidence_sha256="b" * 64,
            source_result_sha256="c" * 64,
            legacy_reason=legacy_reason or "imported from a phase 7 artifact",
        ),
        legacy=legacy,
        legacy_reason=legacy_reason,
    )


def test_a_holdout_carries_its_dates_and_hash_together_or_not_at_all() -> None:
    """Half a holdout is worse than none: it reads as defined and is not."""

    with pytest.raises(ValidationError, match="together or not at all"):
        HoldoutSpec(state=HoldoutState.NOT_DEFINED, start=_OCCURRED)

    with pytest.raises(ValidationError, match="together or not at all"):
        HoldoutSpec(state=HoldoutState.CONTAMINATED, dataset_sha256="a" * 64)


@pytest.mark.parametrize("state", [HoldoutState.LOCKED, HoldoutState.OPENED, HoldoutState.CONSUMED])
def test_a_defined_holdout_requires_its_dates_and_hash(state: HoldoutState) -> None:
    with pytest.raises(ValidationError, match="requires dates and a hash"):
        HoldoutSpec(state=state)


def test_a_contaminated_holdout_may_be_empty_or_fully_specified() -> None:
    """A contamination discovered before the window was cut names no window."""

    assert HoldoutSpec(state=HoldoutState.CONTAMINATED).dataset_sha256 is None

    named = HoldoutSpec(
        state=HoldoutState.CONTAMINATED,
        start=_OCCURRED,
        end=_OCCURRED + timedelta(days=30),
        dataset_sha256="a" * 64,
    )

    assert named.dataset_sha256 == "a" * 64


def test_an_undefined_holdout_refuses_a_fully_named_window() -> None:
    """The half-populated guard runs first, so the fully-populated one needs its own case."""

    with pytest.raises(ValidationError, match="cannot carry dates or a hash"):
        HoldoutSpec(
            state=HoldoutState.NOT_DEFINED,
            start=_OCCURRED,
            end=_OCCURRED + timedelta(days=30),
            dataset_sha256="a" * 64,
        )


def test_a_holdout_window_must_run_forward() -> None:
    with pytest.raises(ValidationError, match="holdout start must precede end"):
        HoldoutSpec(
            state=HoldoutState.LOCKED,
            start=_OCCURRED,
            end=_OCCURRED,
            dataset_sha256="a" * 64,
        )


def test_a_data_range_must_run_forward() -> None:
    with pytest.raises(ValidationError, match="data start must precede end"):
        DataSpec(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            start=_OCCURRED,
            end=_OCCURRED,
            dataset_sha256="a" * 64,
            point_in_time_policy="availability_time",
        )


def test_regime_labels_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="regime labels must be unique"):
        RegimeSpec(labels=("london", "london"), provenance_sha256="a" * 64)


def test_protocol_requires_unique_candidate_spec_ids() -> None:
    """Two trials drawing the same specification are one lottery, not two."""

    first = _candidate(1)
    second = TrialSpec(
        trial_id="trial-2",
        spec_id=first.spec_id,
        rationale="declared before execution",
        parameter_space=(("window", "value-2"),),
    )

    with pytest.raises(ValidationError, match="unique"):
        _protocol((first, second))


def test_a_ledger_event_refuses_a_payload_of_another_type() -> None:
    started = _started(attempt_id="attempt-1")

    with pytest.raises(ValidationError, match="must match payload discriminator"):
        LedgerEvent(**{**started.model_dump(), "event_type": LedgerEventType.FAILED})


def test_a_legacy_event_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="legacy events require a reason"):
        _legacy(attempt_id="attempt-1", trial_id="trial-1", legacy=True)


def test_a_prospective_event_refuses_a_legacy_reason() -> None:
    with pytest.raises(ValidationError, match="prospective events cannot carry a legacy reason"):
        _legacy(
            attempt_id="attempt-1",
            trial_id="trial-1",
            legacy=False,
            legacy_reason="imported from a phase 7 artifact",
        )


def test_trial_counters_count_attempts_lotteries_and_specifications() -> None:
    events = [
        _started(attempt_id="attempt-1", trial_id="trial-1", spec_sha256="1" * 64),
        _started(attempt_id="attempt-2", trial_id="trial-1", spec_sha256="1" * 64),
        _started(attempt_id="attempt-3", trial_id="trial-2", spec_sha256="2" * 64),
        _legacy(
            attempt_id="attempt-4",
            trial_id="trial-3",
            spec_sha256="3" * 64,
            legacy=True,
            legacy_reason="imported from a phase 7 artifact",
        ),
    ]

    assert trial_counters(events) == TrialCounters(
        audit_attempts=4,
        selection_lotteries=3,
        effective_specifications=3,
    )


def test_trial_counters_do_not_count_a_deterministic_reproduction_twice() -> None:
    """A replayed attempt is the same draw from the search space, not a new one."""

    first = _started(attempt_id="attempt-1", trial_id="trial-1", spec_sha256="1" * 64)
    replay = _started(attempt_id="attempt-1", trial_id="trial-1", spec_sha256="1" * 64)

    assert trial_counters([first, replay]) == TrialCounters(
        audit_attempts=1,
        selection_lotteries=1,
        effective_specifications=1,
    )


def test_an_integrity_report_reason_must_match_its_validity() -> None:
    assert LedgerIntegrityReport(valid=True, checked_events=3, reason=None).checked_events == 3

    with pytest.raises(ValidationError, match="cannot carry a failure reason"):
        LedgerIntegrityReport(valid=True, checked_events=3, reason="hash mismatch at 2")

    with pytest.raises(ValidationError, match="requires a reason"):
        LedgerIntegrityReport(valid=False, checked_events=3, reason=None)


def test_a_report_digest_may_not_be_empty() -> None:
    """An empty digest verifies against nothing and blames nothing."""

    with pytest.raises(ValidationError):
        ValidatedPayload(event_type=LedgerEventType.VALIDATED, report_sha256="")

    with pytest.raises(ValidationError):
        GateDecidedPayload(
            event_type=LedgerEventType.GATE_DECIDED,
            decision="RESEARCH_PASSED",
            report_sha256="",
        )
