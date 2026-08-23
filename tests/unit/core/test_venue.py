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
