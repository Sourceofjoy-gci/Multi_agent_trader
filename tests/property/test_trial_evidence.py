"""Invariants the evidence chain rests on: one bundle, one digest, always."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from trading_house.core.exits import NoExitPolicy
from trading_house.core.schemas import Side
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import BacktestResult, SimulatedTrade
from trading_house.research.canonical import canonical_bytes, canonical_sha256
from trading_house.research.evidence import (
    CostSummary,
    DailyReturnPoint,
    EvidenceBundle,
    EvidenceProvenance,
)
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    CostSpec,
    DataSpec,
    ExecutionSpec,
    HoldoutSpec,
    HoldoutState,
    RegimeSpec,
    RegistrationState,
    ReturnSeriesBasis,
    TrialProtocol,
    TrialSpec,
    ValidationSpec,
)

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)

DAY_OFFSETS = st.integers(min_value=0, max_value=32)


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


def _protocol_payload() -> dict[str, Any]:
    """One protocol as a mapping.

    Nested values are instances, not mappings: every research contract is
    ``strict=True`` and a strict model field takes its own model. Only the
    top-level key order is left to permute, which is the point -- canonical
    bytes must not depend on how the caller happened to spell the envelope.
    """

    return {
        "protocol_id": "protocol-1",
        "protocol_version": "1",
        "agent_run_id": "run-1",
        "strategy_id": "strat-1",
        "strategy_version": "v1",
        "strategy_sha256": "a" * 64,
        "data": DataSpec(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M1,
            start=NOW,
            end=NOW + timedelta(hours=1),
            dataset_sha256="f" * 64,
            point_in_time_policy="availability_time",
        ),
        "execution": ExecutionSpec(
            seed="fixed",
            warmup_bars=20,
            fill_policy="pessimistic-bar",
            sizing_policy="risk-engine",
        ),
        "costs": CostSpec(
            baseline=CostModel(
                commission_per_lot_per_side=Decimal("3.50"),
                slippage_points_per_side=Decimal("1"),
                swap_long_points_per_day=Decimal("-1"),
                swap_short_points_per_day=Decimal("-1"),
                triple_swap_weekday=2,
            ),
            stress_multipliers=(Decimal("1.5"), Decimal("2")),
        ),
        "validation": ValidationSpec(
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
        "regimes": RegimeSpec(labels=("london", "new_york"), provenance_sha256="c" * 64),
        "holdout": HoldoutSpec(state=HoldoutState.NOT_DEFINED),
        "candidates": (
            TrialSpec(
                trial_id="trial-1",
                spec_id="spec-1",
                rationale="declared before execution",
                parameter_space=(("window", "20"),),
            ),
        ),
    }


PROTOCOL_KEYS = tuple(_protocol_payload())

# A known answer for one fixed bundle. ``canonical.py`` promises the encoding is
# stable across processes and Python versions, so a digest taken today still
# verifies when the evidence is re-read in a year -- and a digest compared
# against itself cannot show that, because a ``canonical_sha256`` stubbed to 64
# zeros passes it. Only a value recorded here fails when a future ``json`` or
# ``Decimal`` change moves the bytes, which is what the promise needs. Regenerate
# with ``canonical_sha256(_bundle())`` and commit the new value on its own,
# naming the version that moved the encoding.
#
# Restated once, deliberately, when ``EvidenceBundle`` gained ``mark_to_market``
# -- and then *not* restated, which is the point. A plain ``= None`` would have
# written ``"mark_to_market":null`` into every bundle's canonical bytes, moved
# this digest, and made ``EvidenceStore.read`` refuse every v1 document already
# sealed in an operator's store, since it re-serializes what it decoded. The
# field therefore carries ``exclude_if`` and a v1 bundle's bytes are unchanged;
# ``test_a_document_sealed_before_the_series_existed_still_verifies`` in
# ``tests/acceptance/test_phase8b1.py`` proves it by cutting the key out of real
# stored bytes and reading the result back at its own address. This constant did
# not move because that test is green, not because bundle digests are free.
_CANONICAL_BUNDLE_SHA256 = "b07618429d5992d0fb038b6ad53b8401fa1aa9ad467efd06c20421a3557d8558"


def test_one_fixed_bundle_has_a_known_canonical_digest() -> None:
    assert canonical_sha256(_bundle()) == _CANONICAL_BUNDLE_SHA256


@given(DAY_OFFSETS)
def test_bundle_source_digest_matches_its_result(day_offset: int) -> None:
    bundle = _bundle(day_offset=day_offset)
    assert bundle.source_result_sha256 == bundle.result.digest()


@given(st.integers(min_value=0, max_value=32))
def test_a_different_return_day_changes_the_digest(day_offset: int) -> None:
    """The complement of stability: a digest that ignored content would make
    every bundle verify, which is the same defect as a digest that changes on
    identical input."""

    assert canonical_sha256(_bundle(day_offset=day_offset)) != canonical_sha256(
        _bundle(day_offset=day_offset + 1)
    )


@given(st.permutations(PROTOCOL_KEYS))
def test_key_insertion_order_never_changes_canonical_bytes(order: tuple[str, ...]) -> None:
    payload = _protocol_payload()

    shuffled = TrialProtocol(**{key: payload[key] for key in order})

    assert canonical_bytes(shuffled) == canonical_bytes(TrialProtocol(**payload))
