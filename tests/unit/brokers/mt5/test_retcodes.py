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
        (10004, RejectReason.REQUOTE),  # TRADE_RETCODE_REQUOTE
        (10012, RejectReason.TIMEOUT),  # TRADE_RETCODE_TIMEOUT
        (10014, RejectReason.INVALID_QUANTITY),  # TRADE_RETCODE_INVALID_VOLUME
        (10016, RejectReason.INVALID_STOPS),  # TRADE_RETCODE_INVALID_STOPS
        (10017, RejectReason.TRADE_DISABLED),  # TRADE_RETCODE_TRADE_DISABLED
        (10018, RejectReason.MARKET_CLOSED),  # TRADE_RETCODE_MARKET_CLOSED
        (10019, RejectReason.INSUFFICIENT_FUNDS),  # TRADE_RETCODE_NO_MONEY
        (10020, RejectReason.PRICE_CHANGED),  # TRADE_RETCODE_PRICE_CHANGED
        (10024, RejectReason.TIMEOUT),  # TRADE_RETCODE_TOO_MANY_REQUESTS
        (10026, RejectReason.TRADE_DISABLED),  # TRADE_RETCODE_SERVER_DISABLES_AT
        (10027, RejectReason.TRADE_DISABLED),  # TRADE_RETCODE_CLIENT_DISABLES_AT
        (10030, RejectReason.UNSUPPORTED_FILL),  # TRADE_RETCODE_INVALID_FILL
        (10031, RejectReason.DISCONNECTED),  # TRADE_RETCODE_CONNECTION
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
    """10008 PLACED, 10009 DONE, and 10010 DONE_PARTIAL are successes, not
    failures to classify."""

    assert frozenset({10008, 10009, 10010}) == SUCCESS_RETCODES
    assert SUCCESS_RETCODES.isdisjoint(RETCODE_REJECT_REASON)


def test_invalid_price_is_deliberately_unmapped() -> None:
    """10015 has no clean neutral equivalent; mapping it PRICE_CHANGED would
    classify it transient and retry-loop on a genuinely malformed price."""

    assert 10015 not in RETCODE_REJECT_REASON
    assert reject_reason_for(10015) is RejectReason.UNKNOWN


def test_a_genuine_timeout_is_distinct_from_an_invalid_price() -> None:
    """10012 TIMEOUT and 10015 INVALID_PRICE must not collapse into the same
    behaviour: one is transient and retryable, the other is a fail-closed
    unknown that enters safe mode."""

    assert reject_reason_for(10012) is RejectReason.TIMEOUT
    assert recovery_for(reject_reason_for(10012)) is RecoveryAction.RETRY_WITH_FRESH_PRICE

    assert reject_reason_for(10015) is RejectReason.UNKNOWN
    assert recovery_for(reject_reason_for(10015)) is RecoveryAction.ENTER_SAFE_MODE
