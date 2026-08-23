from trading_house.brokers.base import BrokerAdapter
from trading_house.core.venue import RecoveryAction, RejectReason, recovery_for

EXPECTED_METHODS = {
    "describe_instrument",
    "snapshot",
    "precheck",
    "submit",
    "amend_protection",
    "close",
    "reconcile",
    "health",
}


def test_adapter_surface_is_exactly_eight_methods() -> None:
    """A wide adapter is a leaky adapter. Adding a ninth needs a design change."""

    actual = {name for name in vars(BrokerAdapter) if not name.startswith("_")}
    assert actual == EXPECTED_METHODS


def test_adapter_is_a_runtime_checkable_protocol() -> None:
    assert getattr(BrokerAdapter, "_is_protocol", False) is True


def test_every_reject_reason_has_a_recovery_action() -> None:
    for reason in RejectReason:
        assert isinstance(recovery_for(reason), RecoveryAction)


def test_recovery_never_widens_a_stop() -> None:
    """I-15: recovery may not violate the constitution's stop_widening prohibition."""

    assert not any("WIDEN" in action.name for action in RecoveryAction)


def test_invalid_stops_refreshes_the_contract_rather_than_retrying() -> None:
    assert recovery_for(RejectReason.INVALID_STOPS) is RecoveryAction.REFRESH_CONTRACT_AND_RESIZE


def test_authority_failures_enter_safe_mode() -> None:
    assert recovery_for(RejectReason.INSUFFICIENT_FUNDS) is RecoveryAction.ENTER_SAFE_MODE
    assert recovery_for(RejectReason.TRADE_DISABLED) is RecoveryAction.ENTER_SAFE_MODE


def test_transient_failures_retry_with_a_fresh_price() -> None:
    assert recovery_for(RejectReason.REQUOTE) is RecoveryAction.RETRY_WITH_FRESH_PRICE
