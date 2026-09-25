from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from trading_house.core.errors import SchemaValidationError
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.canonical import canonical_bytes, canonical_sha256
from trading_house.research.trial_ledger import (
    CostSpec,
    DataSpec,
    ExecutionSpec,
    HoldoutSpec,
    HoldoutState,
    LedgerEventType,
    RegimeSpec,
    RegistrationState,
    ScopeKind,
    TrialProtocol,
    TrialSpec,
    ValidationSpec,
)


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
