from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.core.schemas import (
    RISK_DECISION_ADAPTER,
    ApprovedRiskDecision,
    OrderIntent,
    PositionState,
    RejectedRiskDecision,
    ResizedRiskDecision,
    Side,
    TradeProposal,
)
from trading_house.core.values import IntentState, PositiveQuantity, Quantity, TimeInForce
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


@pytest.fixture
def valid_intent() -> dict[str, object]:
    return _intent()


@pytest.fixture
def valid_position() -> dict[str, object]:
    return {
        "intent_id": "intent-1",
        "strategy_id": "momentum",
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "side": Side.BUY,
        "quantity": PositiveQuantity(amount=Decimal("0.1"), unit="lots"),
        "open_price": Decimal("1.1"),
        "current_sl": Decimal("1.08"),
        "current_tp": Decimal("1.12"),
        "opened_at_utc": datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        "lifecycle": "OPEN_PROTECTED",
        "r_multiple_open": 0.0,
        "mae_r": -0.1,
        "mfe_r": 0.2,
        "initial_risk_distance": Decimal("0.02"),
        "venue_ref": Mt5VenueRef(venue=Venue.MT5, magic=110001, server_symbol="EURUSD.raw"),
    }


def test_approved_risk_decision_requires_positive_executable_fields() -> None:
    decision = RISK_DECISION_ADAPTER.validate_python(
        {
            "proposal_id": "proposal-1",
            "verdict": "APPROVED",
            "approved_quantity": {"amount": Decimal("0.1"), "unit": "lots"},
            "stop_loss_price": Decimal("1.08"),
            "take_profit_price": Decimal("1.12"),
            "risk_money": Decimal("10.0"),
            "risk_pct_of_book": Decimal("0.35"),
            "reasons": ("within limits",),
            "checks_passed": ("daily loss",),
            "constitution_version": 1,
        }
    )

    assert isinstance(decision, ApprovedRiskDecision)
    assert decision.approved_quantity.amount == Decimal("0.1")


def test_resized_risk_decision_requires_positive_executable_fields() -> None:
    decision = RISK_DECISION_ADAPTER.validate_python(
        {
            "proposal_id": "proposal-1",
            "verdict": "RESIZED",
            "approved_quantity": {"amount": Decimal("0.05"), "unit": "lots"},
            "stop_loss_price": Decimal("1.08"),
            "take_profit_price": None,
            "risk_money": Decimal("5.0"),
            "risk_pct_of_book": Decimal("0.15"),
            "reasons": ("reduced for exposure",),
            "checks_passed": ("daily loss",),
            "constitution_version": 1,
        }
    )

    assert isinstance(decision, ResizedRiskDecision)
    assert decision.risk_money == Decimal("5.0")


def test_rejected_risk_decision_carries_no_executable_data() -> None:
    decision = RISK_DECISION_ADAPTER.validate_python(
        {
            "proposal_id": "proposal-1",
            "verdict": "REJECTED",
            "approved_quantity": {"amount": Decimal("0"), "unit": "lots"},
            "stop_loss_price": None,
            "take_profit_price": None,
            "risk_money": Decimal("0"),
            "risk_pct_of_book": Decimal("0"),
            "reasons": ("daily loss halt",),
            "checks_passed": (),
            "constitution_version": 1,
        }
    )

    assert isinstance(decision, RejectedRiskDecision)


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


def test_position_state_carries_no_venue_encoding() -> None:
    """Mirrors test_order_intent_carries_no_venue_encoding: position_ticket
    duplicated Mt5VenueRef.position_ticket and forced integer position
    identity, which a venue using UUID or string ids cannot satisfy."""

    forbidden = {"position_ticket", "magic", "retcode", "broker_position_ticket"}
    assert forbidden.isdisjoint(PositionState.model_fields)


def test_position_state_requires_a_venue_reference(valid_position: dict[str, object]) -> None:
    """A live position always exists at a venue."""

    valid_position["venue_ref"] = None
    with pytest.raises(ValidationError):
        PositionState(**valid_position)


@pytest.mark.parametrize(
    "field_name",
    ["approved_quantity", "risk_money", "risk_pct_of_book"],
)
def test_rejected_risk_decision_rejects_boolean_zero_fields(field_name: str) -> None:
    """`zero_fields_are_not_booleans` was deleted as redundant (task-6 brief):
    `Quantity` already rejects a boolean amount and strict `Decimal` fields
    reject `bool` outright. This proves that hole did not silently reopen.
    """
    rejected_decision: dict[str, object] = {
        "proposal_id": "proposal-1",
        "verdict": "REJECTED",
        "approved_quantity": {"amount": Decimal("0"), "unit": "lots"},
        "risk_money": Decimal("0"),
        "risk_pct_of_book": Decimal("0"),
        "reasons": ("daily loss halt",),
        "checks_passed": (),
        "constitution_version": 1,
    }
    if field_name == "approved_quantity":
        rejected_decision["approved_quantity"] = {"amount": False, "unit": "lots"}
    else:
        rejected_decision[field_name] = False

    with pytest.raises(ValidationError) as error:
        RISK_DECISION_ADAPTER.validate_python(rejected_decision)

    expected_loc = "amount" if field_name == "approved_quantity" else field_name
    assert error.value.errors()[0]["loc"][-1] == expected_loc


def test_rejected_decision_cannot_carry_executable_quantity() -> None:
    with pytest.raises(ValidationError):
        RISK_DECISION_ADAPTER.validate_python(
            {
                "proposal_id": "p-1",
                "verdict": "REJECTED",
                "approved_quantity": {"amount": Decimal("1.0"), "unit": "lots"},
                "stop_loss_price": Decimal("1.08"),
                "take_profit_price": None,
                "risk_money": Decimal("10.0"),
                "risk_pct_of_book": Decimal("0.35"),
                "reasons": ["daily_loss_halt"],
                "checks_passed": [],
                "constitution_version": 1,
            }
        )


def test_rejected_decision_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="reasons"):
        RISK_DECISION_ADAPTER.validate_python(
            {
                "proposal_id": "p-1",
                "verdict": "REJECTED",
                "approved_quantity": {"amount": Decimal("0"), "unit": "lots"},
                "risk_money": Decimal("0"),
                "risk_pct_of_book": Decimal("0"),
                "reasons": [],
                "checks_passed": [],
                "constitution_version": 1,
            }
        )


def test_order_intent_requires_protective_stop(valid_intent: dict[str, object]) -> None:
    valid_intent.pop("stop_loss")
    with pytest.raises(ValidationError, match="stop_loss"):
        OrderIntent(**valid_intent)


def test_order_intent_rejects_zero_stop(valid_intent: dict[str, object]) -> None:
    valid_intent["stop_loss"] = Decimal("0")
    with pytest.raises(ValidationError, match="greater than 0"):
        OrderIntent(**valid_intent)


def test_order_intent_rejects_unknown_state(valid_intent: dict[str, object]) -> None:
    valid_intent["state"] = "SENT"
    with pytest.raises(ValidationError, match="state"):
        OrderIntent(**valid_intent)


def test_order_intent_carries_no_venue_encoding() -> None:
    forbidden = {
        "magic",
        "deviation_points",
        "filling",
        "retcode",
        "broker_order_ticket",
        "broker_position_ticket",
        "volume",
        "sl",
        "tp",
    }
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
        **{
            **_intent(),
            "t_submit_utc": datetime(2026, 8, 23, 11, 0, tzinfo=timezone(timedelta(hours=2))),
        }
    )
    assert intent.t_submit_utc == datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    assert intent.t_submit_utc.tzinfo is UTC


def test_order_intent_rejects_a_naive_submit_time() -> None:
    from datetime import datetime

    with pytest.raises(ValidationError):
        OrderIntent(**{**_intent(), "t_submit_utc": datetime(2026, 8, 23, 9, 0)})


def test_position_state_requires_positive_initial_risk(valid_position: dict[str, object]) -> None:
    valid_position["initial_risk_distance"] = Decimal("0")
    with pytest.raises(ValidationError, match="greater than 0"):
        PositionState(**valid_position)


def test_position_state_normalizes_open_time_and_is_frozen(
    valid_position: dict[str, object],
) -> None:
    valid_position["opened_at_utc"] = datetime(
        2026, 8, 3, 14, 0, tzinfo=timezone(timedelta(hours=2))
    )
    position = PositionState(**valid_position)

    assert position.opened_at_utc == datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="frozen"):
        position.quantity = PositiveQuantity(amount=Decimal("0.2"), unit="lots")  # type: ignore[misc]


def test_position_state_rejects_unknown_lifecycle(valid_position: dict[str, object]) -> None:
    valid_position["lifecycle"] = "PROTECTED"
    with pytest.raises(ValidationError, match="lifecycle"):
        PositionState(**valid_position)
