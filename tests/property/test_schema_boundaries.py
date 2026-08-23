"""Canonical models must stay strict, closed, frozen, and UTC-disciplined."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from trading_house.audit.models import AuditEvent
from trading_house.brokers.base import (
    MarketSnapshot,
    Quote,
    ReconciliationReport,
    VenueHealth,
)
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

AWARE = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)

STAMP: dict[str, Any] = {
    "event_time": AWARE,
    "availability_time": AWARE,
    "processing_time": AWARE,
    "source": "property-suite",
}

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
        "position_ticket": 1,
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
        "venue_ref": None,
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
}

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


def _canonical_models() -> set[type[BaseModel]]:
    pending: list[type[BaseModel]] = [CanonicalModel]
    found: set[type[BaseModel]] = set()
    while pending:
        for subclass in pending.pop().__subclasses__():
            found.add(subclass)
            pending.append(subclass)
    return found


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
