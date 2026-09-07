"""Tests for the write-before-send order manager.

The property this whole phase exists to guarantee --  that a lost response
must never double a position -- is proven by
``test_a_lost_response_records_unknown_and_does_not_resend``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tests.unit.execution.conftest import FakeVenue, RecordingLedger
from trading_house.core.clock import FixedClock
from trading_house.core.schemas import OrderIntent, Side
from trading_house.core.values import IntentState, PositiveQuantity, TimeInForce
from trading_house.core.venue import RecoveryAction, RejectReason
from trading_house.execution.manager import OrderManager

NOW = datetime(2026, 8, 25, tzinfo=UTC)
STRATEGY_ID = "trend_following"


def _intent(*, quantity: Decimal = Decimal("0.10")) -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        proposal_id="proposal-1",
        book="fx_scalp",
        instrument_id="fx.eurusd",
        side=Side.BUY,
        quantity=PositiveQuantity(amount=quantity, unit="lots"),
        stop_loss=Decimal("1.0950"),
        take_profit=None,
        time_in_force=TimeInForce.IOC,
        max_slippage_bps=Decimal("2"),
        state=IntentState.SUBMITTING,
        t_submit_utc=NOW,
        venue_ref=None,
        outcome=None,
    )


def test_the_ledger_records_submitting_before_the_venue_is_called() -> None:
    """The durability point. If the write happened after the send, a crash in
    between would leave a broker position with no ledger entry at all, and
    nothing would ever look for it."""

    order: list[str] = []
    ledger = RecordingLedger(on_append=lambda state: order.append(f"ledger:{state.value}"))
    venue = FakeVenue()
    venue_submit = venue.submit

    def watched(intent: OrderIntent) -> Any:
        order.append("venue:submit")
        return venue_submit(intent)

    venue.submit = watched  # type: ignore[method-assign]
    OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent(), STRATEGY_ID)

    assert order[0] == "ledger:SUBMITTING"
    assert order[1] == "venue:submit"


def test_an_accepted_submission_records_confirmed_once() -> None:
    ledger, venue = RecordingLedger(), FakeVenue()

    state = OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent(), STRATEGY_ID)

    assert state is IntentState.CONFIRMED
    assert [s for _, s, _ in ledger.appended] == [IntentState.SUBMITTING, IntentState.CONFIRMED]


def test_a_rejection_records_its_reason_and_recovery() -> None:
    ledger, venue = RecordingLedger(), FakeVenue(behaviour="reject")

    state = OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent(), STRATEGY_ID)

    assert state is IntentState.REJECTED
    payload = ledger.appended[-1][2]
    assert payload["reject_reason"] == RejectReason.INSUFFICIENT_FUNDS.value
    assert payload["recovery"] == RecoveryAction.ENTER_SAFE_MODE.value


def test_a_lost_response_records_unknown_and_does_not_resend() -> None:
    """The flagship. The broker executed; the answer was lost. Resending here
    is what doubles a position, and it is the one thing that must never
    happen."""

    ledger, venue = RecordingLedger(), FakeVenue(behaviour="lost_after_execution")

    state = OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent(), STRATEGY_ID)

    assert state is IntentState.UNKNOWN
    assert len(venue.calls) == 1
    assert len(venue.executed) == 1


def test_the_manager_never_reconciles() -> None:
    """submit() records UNKNOWN and stops. Reconciliation belongs to the
    reconciler, so that the path recovering a lost response is the same code
    that recovers after a crash -- and is therefore exercised routinely."""

    ledger, venue = RecordingLedger(), FakeVenue(behaviour="lost_after_execution")

    OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent(), STRATEGY_ID)

    assert [s for _, s, _ in ledger.appended] == [IntentState.SUBMITTING, IntentState.UNKNOWN]


def test_a_partial_fill_is_confirmed_at_the_filled_quantity() -> None:
    """Retcode 10010 is a partial fill. Recording the requested quantity
    would put a position size in the ledger that the broker never gave us."""

    ledger = RecordingLedger()
    venue = FakeVenue(behaviour="partial")  # fills half

    state = OrderManager(ledger, venue, FixedClock(NOW)).submit(
        _intent(quantity=Decimal("0.50")), STRATEGY_ID
    )

    assert state is IntentState.CONFIRMED
    assert ledger.appended[-1][2]["filled_quantity"] == "0.25"
    assert ledger.appended[-1][2]["requested_quantity"] == "0.50"


def test_the_submitting_snapshot_records_strategy_id() -> None:
    """OrderIntent does not carry strategy_id. Task 5's reconcile() builds a
    PositionState by reading it back out of this SUBMITTING payload --
    execution/ has no access to the proposal it came from."""

    ledger, venue = RecordingLedger(), FakeVenue()

    OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent(), STRATEGY_ID)

    assert ledger.appended[0][2]["strategy_id"] == STRATEGY_ID


def test_every_decimal_in_the_snapshot_is_a_string() -> None:
    """A Decimal that reaches JSONB as a float is silent corruption of the
    money path."""

    ledger, venue = RecordingLedger(), FakeVenue()

    OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent(), STRATEGY_ID)

    snapshot = ledger.appended[0][2]
    assert snapshot["quantity"] == "0.10"
    assert snapshot["stop_loss"] == "1.0950"
    assert snapshot["max_slippage_bps"] == "2"
