"""Canonical models must stay strict, closed, frozen, and UTC-disciplined."""

from __future__ import annotations

import importlib
import pkgutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, get_args
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from trading_house.agents.providers.base import AgentRun, RunOutcome
from trading_house.audit.models import AuditEvent, AuditRecord
from trading_house.brokers.base import (
    MarketSnapshot,
    Quote,
    ReconciliationReport,
    VenueHealth,
)
from trading_house.core.exits import NoExitPolicy
from trading_house.core.schemas import (
    AgentOpinion,
    CanonicalModel,
    OrderIntent,
    PositionState,
    RegimeAssessment,
    Side,
    TradeProposal,
)
from trading_house.core.values import IntentState, PositiveQuantity, Quantity, TimeInForce
from trading_house.core.venue import Mt5VenueRef, Venue
from trading_house.features.sessions import Session
from trading_house.marketdata.models import (
    Bar,
    BarQuality,
    Coverage,
    IngestOutcome,
    IngestRun,
    Timeframe,
)
from trading_house.memory.models import AgentBelief, MemoryStore, ObservedFact, WriterKind
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import BacktestResult, SimulatedTrade
from trading_house.research.backtest.snapshot import FeatureSnapshot
from trading_house.research.trial_ledger import (
    CostSpec,
    DataSpec,
    ExecutionSpec,
    ExecutionStartedPayload,
    HoldoutSpec,
    HoldoutState,
    LedgerEvent,
    LedgerEventType,
    LedgerRecord,
    PreregisteredPayload,
    RegimeSpec,
    RegistrationState,
    ScopeKind,
    TrialProtocol,
    TrialSpec,
    ValidationSpec,
)

AWARE = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)

STAMP: dict[str, Any] = {
    "event_time": AWARE,
    "availability_time": AWARE,
    "processing_time": AWARE,
    "source": "property-suite",
}


def _protocol() -> TrialProtocol:
    """One complete protocol for the LedgerEvent builder below.

    Built as an instance, not a nested mapping: every research contract is
    ``strict=True``, and a strict model field takes its own model.
    """

    return TrialProtocol(
        protocol_id="protocol-1",
        protocol_version="1",
        agent_run_id="run-1",
        strategy_id="session_momentum",
        strategy_version="1",
        strategy_sha256="a" * 64,
        data=DataSpec(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            start=AWARE,
            end=AWARE + timedelta(days=1),
            dataset_sha256="b" * 64,
            point_in_time_policy="availability_time",
        ),
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
        candidates=(
            TrialSpec(
                trial_id="trial-1",
                spec_id="spec-1",
                rationale="declared before execution",
                parameter_space=(("window", "20"),),
            ),
        ),
    )


BUILDERS: dict[type[BaseModel], dict[str, Any]] = {
    RegimeAssessment: {
        **STAMP,
        "symbol": "EURUSD",
        "volatility_state": "normal",
        "trend_state": "up",
        "liquidity_state": "normal",
        "probabilities": {"up": 1.0},
        "uncertainty": 0.5,
    },
    TradeProposal: {
        **STAMP,
        "proposal_id": "p-1",
        "strategy_id": "s-1",
        "strategy_version": "1",
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "side": Side.BUY,
        "horizon_seconds": 60,
        "entry_condition": "breakout",
        "entry_price_ref": Decimal("1.1"),
        "invalidation_price": Decimal("1.05"),
        "max_holding_seconds": 600,
        "expected_return_bps": 5.0,
        "expected_return_stdev_bps": 2.0,
        "expected_cost_bps": 1.0,
        "expected_swap_cost_bps": 0.0,
        "win_probability": 0.55,
        "calibration_id": "c-1",
        "required_liquidity": PositiveQuantity(amount=Decimal("0.5"), unit="lots"),
        "regime_ref": "r-1",
        "features_snapshot_id": "f-1",
    },
    AgentOpinion: {
        **STAMP,
        "agent_role": "technical",
        "subject_id": "p-1",
        "stance": "FOR",
        "confidence": 0.6,
    },
    OrderIntent: {
        "intent_id": "i-1",
        "proposal_id": "p-1",
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "side": Side.BUY,
        "quantity": PositiveQuantity(amount=Decimal("0.1"), unit="lots"),
        "stop_loss": Decimal("1.05"),
        "take_profit": None,
        "time_in_force": TimeInForce.IOC,
        "max_slippage_bps": Decimal("2"),
        "state": IntentState.SUBMITTING,
        "t_submit_utc": AWARE,
        "venue_ref": None,
        "outcome": None,
    },
    PositionState: {
        "intent_id": "i-1",
        "strategy_id": "s-1",
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "side": Side.BUY,
        "quantity": PositiveQuantity(amount=Decimal("0.1"), unit="lots"),
        "open_price": Decimal("1.1"),
        "current_sl": Decimal("1.05"),
        "current_tp": None,
        "opened_at_utc": AWARE,
        "lifecycle": "OPEN_PROTECTED",
        "r_multiple_open": 0.0,
        "mae_r": 0.0,
        "mfe_r": 0.0,
        "initial_risk_distance": Decimal("0.05"),
        "venue_ref": Mt5VenueRef(venue=Venue.MT5, magic=110001, server_symbol="EURUSD.raw"),
    },
    AuditEvent: {
        "schema_version": 1,
        "event_id": uuid4(),
        "event_type": "property.event",
        "occurred_at": AWARE,
        "actor": "property-suite",
        "actor_type": "test",
        "payload": {"ok": True},
    },
    AuditRecord: {
        "sequence_number": 1,
        "event_id": uuid4(),
        "canonical_event": b"{}",
        "event_json": {"ok": True},
        "previous_hash": bytes(32),
        "entry_hash": bytes(32),
        "received_at": AWARE,
    },
    AgentRun: {
        "run_id": "run-1",
        "task_id": "task-1",
        "provider_id": "provider-1",
        "model_id": "model-1",
        "prompt_sha256": "prompt-hash",
        "transcript_sha256": "transcript-hash",
        "diff_sha256": None,
        "outcome": RunOutcome.COMPLETED,
        "tokens_used": 100,
        "cost_usd_millis_used": 100,
        "tool_calls_used": 1,
        "started_at": AWARE,
        "finished_at": AWARE,
    },
    Quote: {
        "instrument_id": "fx.eurusd",
        "bid": Decimal("1.1"),
        "ask": Decimal("1.2"),
        "observed_at": AWARE,
    },
    MarketSnapshot: {
        "quotes": (),
        "taken_at": AWARE,
    },
    VenueHealth: {
        "connected": True,
        "server_utc_offset_seconds": 0,
        "last_quote_age_seconds": 0,
    },
    ReconciliationReport: {
        "book": "fx_scalp",
        "positions": (),
        "unmatched_venue_refs": (),
        "reconciled_at": AWARE,
    },
    ObservedFact: {
        "fact_id": "f-1",
        "store": MemoryStore.A,
        "written_by": WriterKind.DETERMINISTIC,
        "instrument_id": "fx.eurusd",
        "metric": "slippage_bps",
        "value": 1.5,
        "observed_at": AWARE,
        "availability_time": AWARE,
    },
    AgentBelief: {
        "belief_id": "b-1",
        "store": MemoryStore.B,
        "agent_run_id": "r-1",
        "claim": "EURUSD trends after London open",
        "availability_time": AWARE,
    },
    Bar: {
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "event_time": AWARE,
        "availability_time": AWARE + timedelta(minutes=1),
        "open": Decimal("1.10000"),
        "high": Decimal("1.10050"),
        "low": Decimal("1.09950"),
        "close": Decimal("1.10020"),
        "tick_volume": 42,
        "spread": 9,
        "real_volume": 0,
        "quality": BarQuality.OK,
    },
    Coverage: {
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "earliest_event_time": AWARE,
        "latest_event_time": AWARE,
        "latest_availability_time": AWARE + timedelta(minutes=1),
        "clean_bars": 10,
        "defective_bars": 0,
    },
    IngestRun: {
        "run_id": uuid4(),
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "requested_from": AWARE,
        "requested_to": AWARE,
        "started_at": AWARE,
        "finished_at": AWARE,
        "earliest_event_time": AWARE,
        "bars_returned": 10,
        "bars_stored": 10,
        "bars_rejected": 0,
        "bars_conflicting": 0,
        "expected_bars": 10,
        "coverage_ratio": Decimal("1.0"),
        "outcome": IngestOutcome.COMPLETE,
        "detail": None,
    },
    FeatureSnapshot: {
        "as_of": AWARE + timedelta(minutes=1),
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "bar": Bar(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M1,
            event_time=AWARE,
            availability_time=AWARE + timedelta(minutes=1),
            open=Decimal("1.10000"),
            high=Decimal("1.10050"),
            low=Decimal("1.09950"),
            close=Decimal("1.10020"),
            tick_volume=42,
            spread=9,
            real_volume=0,
            quality=BarQuality.OK,
        ),
        "atr": Decimal("0.00050"),
        "median_spread_points": Decimal("9"),
        "tick_spread_points": Decimal("9"),
        "tick_time": AWARE + timedelta(minutes=1),
        "session": Session.LONDON,  # AWARE's hour is 9, inside London's 7-16
        "prior_session_return": None,
        "session_open_price": Decimal("1.10000"),
        "bars_since_session_open": 0,
    },
    SimulatedTrade: {
        "proposal_id": "p-1",
        "side": Side.BUY,
        "lots": Decimal("0.10"),
        "entry_price": Decimal("1.10000"),
        "entry_at": AWARE,
        "exit_price": Decimal("1.10100"),
        "exit_at": AWARE + timedelta(minutes=12),
        "exit_kind": ExitKind.TIME,
        "gross_pnl": Decimal("100"),
        "commission": Decimal("7"),
        "swap": Decimal("-3"),
        "net_pnl": Decimal("90"),
    },
    BacktestResult: {
        "run_id": "run-1",
        "strategy_id": "strat-1",
        "strategy_version": "v1",
        "exit_policy": NoExitPolicy(kind="none"),
        "constitution_sha256": "a" * 64,
        "contract_sha256": "b" * 64,
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "start": AWARE,
        "end": AWARE + timedelta(hours=1),
        "firm_equity": Decimal("100000"),
        "cost_model": CostModel(
            commission_per_lot_per_side=Decimal("3.50"),
            slippage_points_per_side=Decimal("1"),
            swap_long_points_per_day=Decimal("-1"),
            swap_short_points_per_day=Decimal("-1"),
            triple_swap_weekday=2,
        ),
        "atr_period": 14,
        "spread_window": 20,
        "defective_bar_tolerance": Fraction(0),
        "trades": (),  # filled in below: SimulatedTrade's own entry is not bound yet here
        "rejections": (),
        "bars_seen": 60,
        "snapshots_skipped": 0,
        "net_pnl": Decimal("90"),
    },
    DataSpec: {
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M15,
        "start": AWARE,
        "end": AWARE + timedelta(days=1),
        "dataset_sha256": "b" * 64,
        "point_in_time_policy": "availability_time",
    },
    # NOT_DEFINED is the only state that must carry no dates, so this builder
    # leaves both timestamps and the hash None rather than inventing a window.
    HoldoutSpec: {
        "state": HoldoutState.NOT_DEFINED,
        "start": None,
        "end": None,
        "dataset_sha256": None,
    },
    ExecutionStartedPayload: {
        "event_type": LedgerEventType.EXECUTION_STARTED,
        "attempt_id": "attempt-1",
        "execution_started_at": AWARE,
    },
    LedgerEvent: {
        "event_id": uuid4(),
        "scope_kind": ScopeKind.PROTOCOL,
        "scope_id": "protocol-1",
        "event_type": LedgerEventType.PREREGISTERED,
        "trial_id": None,
        "attempt_id": None,
        "spec_sha256": "a" * 64,
        "occurred_at": AWARE,
        "payload": PreregisteredPayload(
            event_type=LedgerEventType.PREREGISTERED,
            protocol=_protocol(),
            registration_state=RegistrationState.PROSPECTIVE,
        ),
        "legacy": False,
        "legacy_reason": None,
    },
    # Every non-optional field is supplied: LedgerRecord has no defaults, so a
    # partial builder would fail test_every_builder_produces_a_valid_model.
    LedgerRecord: {
        "sequence": 1,
        "event_id": uuid4(),
        "scope_kind": ScopeKind.PROTOCOL,
        "scope_id": "protocol-1",
        "trial_id": None,
        "attempt_id": None,
        "event_type": LedgerEventType.PREREGISTERED,
        "spec_sha256": "a" * 64,
        "event_json": {},
        "payload_sha256": "b" * 64,
        "previous_hash": "0" * 64,
        "event_hash": "c" * 64,
        "legacy": False,
        "legacy_reason": None,
        "recorded_at": AWARE,
    },
}

BUILDERS[BacktestResult]["trades"] = (SimulatedTrade(**BUILDERS[SimulatedTrade]),)
# One source of truth for a trade's shape: BUILDERS is not bound during its own
# literal's evaluation, so this couldn't be `SimulatedTrade(**BUILDERS[SimulatedTrade])`
# inline above -- it must be resolved after the dict exists instead of duplicating
# SimulatedTrade's fields a second time.

MODELS = sorted(BUILDERS, key=lambda model: model.__name__)


def _datetime_fields(model: type[BaseModel]) -> list[str]:
    return [
        name
        for name, value in BUILDERS[model].items()
        if isinstance(value, datetime) and name in model.model_fields
    ]


DATETIME_CASES = sorted(
    ((model, field) for model in MODELS for field in _datetime_fields(model)),
    key=lambda case: (case[0].__name__, case[1]),
)


UNIMPORTABLE_OFF_WINDOWS: frozenset[str] = frozenset(
    {
        # Imports ``MetaTrader5`` at module scope, and that package ships a
        # ``sys_platform == 'win32'`` wheel only (pyproject.toml) -- it does
        # not exist to import on the Linux CI job. This is the one module the
        # package's own architecture guards (``brokers/mt5/__init__.py``'s
        # docstring, ``tests/acceptance/test_phase1.py``'s ORDER_SEND_ALLOWED)
        # allow to import it. Named here, not swallowed: an import failure
        # outside this set is a real regression and must fail the test below.
        "trading_house.brokers.mt5.terminal",
    }
)


def _import_every_module() -> None:
    """Import the whole package so subclass discovery cannot miss a model.

    ``CanonicalModel.__subclasses__()`` only sees classes whose module has been
    imported. That made this file's guard answer differently depending on which
    other tests shared the process -- it passed alone and failed after
    ``tests/unit``, and Phase 6 shipped three unregistered models through the
    gap. Importing the package first removes the dependency on test ordering.

    One module cannot be imported on every platform -- see
    ``UNIMPORTABLE_OFF_WINDOWS``. Catching ``ImportError`` unconditionally and
    moving on would reopen exactly the hole this file exists to close: a
    module that quietly stops importing (for any reason, not just a missing
    platform wheel) would hide its models again with nothing to say so. So a
    failure is recorded, not swallowed, and checked against the declared set --
    a failure outside it fails this test instead of vanishing silently.
    """

    import trading_house

    skipped: set[str] = set()
    for module in pkgutil.walk_packages(trading_house.__path__, f"{trading_house.__name__}."):
        try:
            importlib.import_module(module.name)
        except ImportError:
            skipped.add(module.name)

    unexpected = skipped - UNIMPORTABLE_OFF_WINDOWS
    assert not unexpected, (
        f"unexpected import failure(s) during subclass discovery: {sorted(unexpected)}"
    )


def _canonical_models() -> set[type[BaseModel]]:
    _import_every_module()
    pending: list[type[BaseModel]] = [CanonicalModel]
    found: set[type[BaseModel]] = set()
    while pending:
        for subclass in pending.pop().__subclasses__():
            found.add(subclass)
            pending.append(subclass)
    return found


def _declares_a_datetime_field(model: type[BaseModel]) -> bool:
    """True if any of the model's fields is (or contains) ``datetime``.

    Handles both a bare ``datetime`` annotation and ``datetime | None`` so a
    future optional timestamp field does not slip past this check.
    """

    return any(
        field.annotation is datetime or datetime in get_args(field.annotation)
        for field in model.model_fields.values()
    )


def test_every_datetime_bearing_canonical_model_has_a_builder() -> None:
    """Close the hole, not just the instance (I-10, CRITICAL fix-wave finding 1).

    A model can ship with a live naive-datetime hole simply by never being
    registered in ``BUILDERS`` -- ``test_naive_datetimes_are_rejected_everywhere``
    is vacuous for anything missing from that dict. This walks the same
    subclass tree ``_canonical_models()`` uses and demands a builder for every
    *concrete* (leaf) model that declares a datetime field. Abstract bases
    that exist only to be subclassed (``Stamped``, ``_PointInTime``,
    ``AuditModel``) are skipped: they have no builder of their own because
    nothing constructs them directly -- their concrete subclasses are checked
    instead, and each one already appears in ``BUILDERS``.
    """

    offenders = sorted(
        model.__name__
        for model in _canonical_models()
        if not model.__subclasses__()  # leaf: no concrete subclass will be checked instead
        and _declares_a_datetime_field(model)
        and model not in BUILDERS
    )

    assert offenders == []


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__name__)
def test_every_builder_produces_a_valid_model(model: type[BaseModel]) -> None:
    """Guard the guard: a broken builder would make the tests below vacuous."""

    assert isinstance(model(**BUILDERS[model]), model)


@pytest.mark.parametrize(
    ("model", "field"), DATETIME_CASES, ids=lambda value: getattr(value, "__name__", str(value))
)
def test_naive_datetimes_are_rejected_everywhere(model: type[BaseModel], field: str) -> None:
    """Invariant I-10, enforced on every canonical datetime field."""

    payload = dict(BUILDERS[model])
    payload[field] = payload[field].replace(tzinfo=None)

    with pytest.raises(ValidationError):
        model(**payload)


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__name__)
@given(name=st.text(min_size=1, max_size=12).filter(lambda text: not text.startswith("_")))
def test_unexpected_fields_are_always_rejected(model: type[BaseModel], name: str) -> None:
    if name in model.model_fields:
        return
    payload = {**BUILDERS[model], name: "surprise"}

    with pytest.raises(ValidationError, match="Extra inputs"):
        model(**payload)


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__name__)
def test_every_model_is_frozen(model: type[BaseModel]) -> None:
    instance = model(**BUILDERS[model])
    field = next(iter(model.model_fields))

    with pytest.raises(ValidationError):
        setattr(instance, field, None)


def test_every_canonical_subclass_is_strict_closed_and_frozen() -> None:
    subclasses = _canonical_models()

    assert len(subclasses) >= len(MODELS)
    for model in subclasses:
        config = model.model_config
        assert config.get("frozen") is True, model.__name__
        assert config.get("extra") == "forbid", model.__name__
        assert config.get("strict") is True, model.__name__


@given(st.integers(min_value=-(10**6), max_value=10**6))
def test_strict_mode_rejects_string_numbers(value: int) -> None:
    payload = {**BUILDERS[TradeProposal], "horizon_seconds": str(value)}

    with pytest.raises(ValidationError):
        TradeProposal(**payload)


@given(st.sampled_from([float("nan"), float("inf"), float("-inf")]))
def test_non_finite_prices_are_rejected(value: float) -> None:
    payload = {**BUILDERS[TradeProposal], "entry_price_ref": value}

    with pytest.raises(ValidationError):
        TradeProposal(**payload)


def test_order_intent_requires_a_positive_quantity_type() -> None:
    """A zero-permitting Quantity must not satisfy OrderIntent.quantity."""
    payload = {**BUILDERS[OrderIntent], "quantity": Quantity(amount=Decimal("0"), unit="lots")}

    with pytest.raises(ValidationError):
        OrderIntent(**payload)


def test_order_intent_quantity_is_annotated_positive() -> None:
    assert OrderIntent.model_fields["quantity"].annotation is PositiveQuantity
