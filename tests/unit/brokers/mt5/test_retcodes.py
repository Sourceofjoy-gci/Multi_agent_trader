import pytest

from trading_house.brokers.mt5.retcodes import (
    RETCODE_REJECT_REASON,
    SUCCESS_RETCODES,
    reject_reason_for,
)
from trading_house.core.venue import REJECT_CLASS, RecoveryAction, RejectReason, recovery_for


@pytest.mark.parametrize(
    ("retcode", "expected"),
    [
        (10004, RejectReason.REQUOTE),
        (10014, RejectReason.INVALID_QUANTITY),
        (10016, RejectReason.INVALID_STOPS),
        (10018, RejectReason.MARKET_CLOSED),
        (10019, RejectReason.INSUFFICIENT_FUNDS),
        (10020, RejectReason.PRICE_CHANGED),
        (10024, RejectReason.TIMEOUT),
        (10026, RejectReason.TRADE_DISABLED),
        (10027, RejectReason.TRADE_DISABLED),
        (10030, RejectReason.UNSUPPORTED_FILL),
        (10031, RejectReason.DISCONNECTED),
    ],
)
def test_documented_retcodes_map_to_their_neutral_reason(
    retcode: int, expected: RejectReason
) -> None:
    assert reject_reason_for(retcode) is expected


@pytest.mark.parametrize("retcode", [0, 1, 10015, 10021, 10099, 99999, -1])
def test_unrecognised_retcodes_fail_closed(retcode: int) -> None:
    """Guessing an unknown code as transient would produce a retry loop."""

    assert reject_reason_for(retcode) is RejectReason.UNKNOWN
    assert recovery_for(reject_reason_for(retcode)) is RecoveryAction.ENTER_SAFE_MODE


def test_every_mapped_reason_has_a_recovery_action() -> None:
    for reason in RETCODE_REJECT_REASON.values():
        assert reason in REJECT_CLASS


def test_success_retcodes_are_not_treated_as_rejections() -> None:
    """10008 PLACED and 10009 DONE are successes, not failures to classify."""

    assert frozenset({10008, 10009}) == SUCCESS_RETCODES
    assert SUCCESS_RETCODES.isdisjoint(RETCODE_REJECT_REASON)


def test_invalid_price_is_deliberately_unmapped() -> None:
    """10015 has no clean neutral equivalent; mapping it PRICE_CHANGED would
    classify it transient and retry-loop on a genuinely malformed price."""

    assert 10015 not in RETCODE_REJECT_REASON
    assert reject_reason_for(10015) is RejectReason.UNKNOWN
