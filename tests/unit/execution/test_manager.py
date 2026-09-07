"""Tests for the write-before-send order manager.

The property this whole phase exists to guarantee --  that a lost response
must never double a position -- is proven by
``test_a_lost_response_records_unknown_and_does_not_resend``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from tests.unit.execution.conftest import NOW, FakeVenue, RecordingLedger, _intent
from trading_house.core.clock import FixedClock
from trading_house.core.errors import IntentAlreadySubmittedError
from trading_house.core.schemas import OrderIntent
from trading_house.core.values import IntentState
from trading_house.core.venue import Mt5VenueRef, RecoveryAction, RejectReason, Venue
from trading_house.execution.manager import OrderManager

STRATEGY_ID = "trend_following"


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


def test_a_response_lost_before_execution_still_does_not_resend() -> None:
    """A ConnectionError raised while reading the response looks exactly like
    one raised before the request ever left the box -- nothing in the
    exception says which. §3.6 forbids treating "it never left" as knowable
    from the exception alone, so even this case, which *looks* safest to
    retry, must be recorded UNKNOWN and never resent."""

    ledger, venue = RecordingLedger(), FakeVenue(behaviour="lost_before_execution")

    state = OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent(), STRATEGY_ID)

    assert state is IntentState.UNKNOWN
    assert len(venue.calls) == 1


def test_a_second_submit_for_the_same_intent_id_is_refused() -> None:
    """submit() is not a retry mechanism. Once an intent_id has any ledger
    event, a second submit() call must be refused before it ever writes or
    calls the venue again -- a caller wanting a genuine retry mints a fresh
    intent_id (§3.6)."""

    ledger, venue = RecordingLedger(), FakeVenue()
    manager = OrderManager(ledger, venue, FixedClock(NOW))

    manager.submit(_intent(), STRATEGY_ID)
    with pytest.raises(IntentAlreadySubmittedError):
        manager.submit(_intent(), STRATEGY_ID)

    assert len(venue.calls) == 1


def test_the_submitting_snapshot_records_venue_ref_and_submit_time() -> None:
    """Task 4's reconciliation sweep reads magic, server symbol and submit
    time back out of the SUBMITTING payload -- it is the only payload an
    UNKNOWN intent ever gets. execution/ does not derive these; the caller
    populates ``venue_ref`` before calling submit()."""

    ledger, venue = RecordingLedger(), FakeVenue()
    venue_ref = Mt5VenueRef(venue=Venue.MT5, magic=110042, server_symbol="EURUSD")

    OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent(venue_ref=venue_ref), STRATEGY_ID)

    snapshot = ledger.appended[0][2]
    assert snapshot["venue_ref"]["magic"] == 110042
    assert snapshot["venue_ref"]["server_symbol"] == "EURUSD"
    assert snapshot["t_submit_utc"] == NOW.isoformat()


def test_the_submitting_snapshot_allows_a_null_venue_ref() -> None:
    """Populating venue_ref is the caller's job, not this task's. An intent
    submitted with no venue_ref must still submit cleanly and record
    ``venue_ref: null`` rather than requiring one."""

    ledger, venue = RecordingLedger(), FakeVenue()

    state = OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent(), STRATEGY_ID)

    assert state is IntentState.CONFIRMED
    assert ledger.appended[0][2]["venue_ref"] is None
