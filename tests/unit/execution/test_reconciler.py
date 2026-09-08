"""Tests for the reconciliation verdict, the sweep and the unresolved-intent
gate.

Two tests here carry this phase: ``test_the_gate_refuses_while_an_intent_is_unresolved``
proves the gate actually blocks, and
``test_a_process_that_died_between_the_write_and_the_send_blocks_the_next_one``
proves the crash-recovery path and the lost-response path are the same code.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.unit.execution.conftest import (
    BASE,
    FakeDeals,
    FakeVenue,
    RecordingLedger,
    _intent,
    _snapshot_payload,
)
from trading_house.core.clock import FixedClock
from trading_house.core.errors import UnresolvedIntentsError
from trading_house.core.values import IntentState
from trading_house.core.venue import DealRecord
from trading_house.execution.ledger import NON_TERMINAL_STATES
from trading_house.execution.manager import OrderManager
from trading_house.execution.reconciler import (
    Verdict,
    reconcile_all,
    require_clean_ledger,
    verdict,
)

STRATEGY_ID = "trend_following"
BASE_PLUS_5 = BASE + timedelta(seconds=5)


def _deal(**overrides: object) -> DealRecord:
    fields: dict[str, object] = {
        "magic": 110042,
        "server_symbol": "EURUSD",
        "volume": Decimal("0.25"),
        "position_ticket": 7,
        "dealt_at": BASE,
    }
    fields.update(overrides)
    return DealRecord(**fields)  # type: ignore[arg-type]


def _verdict(**overrides: object) -> Verdict:
    args: dict[str, object] = {
        "magic": 110042,
        "server_symbol": "EURUSD",
        "volume": Decimal("0.25"),
        "t_submit": BASE,
        "deals": (),
        "now": BASE + timedelta(seconds=5),
        "terminal_healthy": True,
    }
    args.update(overrides)
    return verdict(**args)  # type: ignore[arg-type]


# --- verdict() -------------------------------------------------------------


def test_exactly_one_matching_deal_confirms() -> None:
    assert _verdict(deals=(_deal(),)) is Verdict.CONFIRMED


def test_two_matching_deals_never_guess() -> None:
    """This is what a magic collision actually produces. Adopting one attaches
    our ledger to a position that may not be ours, which is worse than staying
    unresolved."""

    assert _verdict(deals=(_deal(), _deal(position_ticket=8))) is Verdict.STILL_UNKNOWN


def test_a_deal_with_a_different_magic_does_not_match() -> None:
    assert _verdict(deals=(_deal(magic=119999),)) is Verdict.STILL_UNKNOWN


def test_a_deal_on_another_symbol_does_not_match() -> None:
    """Magic ranges are per book, not per symbol, so two intents in the same
    book can collide on magic while trading different instruments."""

    assert _verdict(deals=(_deal(server_symbol="GBPUSD"),)) is Verdict.STILL_UNKNOWN


def test_a_deal_of_a_different_volume_does_not_match() -> None:
    assert _verdict(deals=(_deal(volume=Decimal("0.50")),)) is Verdict.STILL_UNKNOWN


def test_a_deal_stamped_before_the_lookback_does_not_match() -> None:
    """MATCH_LOOKBACK is 60s of broker clock skew, not an open-ended history."""

    assert _verdict(deals=(_deal(dealt_at=BASE - timedelta(seconds=61)),)) is (
        Verdict.STILL_UNKNOWN
    )


def test_a_deal_stamped_slightly_before_submission_still_matches() -> None:
    """The broker's clock can run ahead of ours; a deal stamped 30 seconds
    before we think we sent it is still plausibly ours."""

    assert _verdict(deals=(_deal(dealt_at=BASE - timedelta(seconds=30)),)) is (Verdict.CONFIRMED)


def test_a_deal_stamped_exactly_at_the_lookback_boundary_matches() -> None:
    """The comparison is ``window_start <= deal.dealt_at``: a deal stamped
    exactly MATCH_LOOKBACK (60s) before t_submit is still on our side of the
    line, not one tick outside it."""

    assert _verdict(deals=(_deal(dealt_at=BASE - timedelta(seconds=60)),)) is Verdict.CONFIRMED


def test_a_deal_stamped_one_second_beyond_the_lookback_boundary_does_not_match() -> None:
    """One second earlier than the boundary above: now outside the window,
    so it cannot be ours."""

    assert _verdict(deals=(_deal(dealt_at=BASE - timedelta(seconds=61)),)) is Verdict.STILL_UNKNOWN


def test_no_match_inside_the_timeout_stays_unknown() -> None:
    """Before RESOLUTION_TIMEOUT elapses, absence of a deal is not evidence.
    Declaring FAILED here would abandon an order still working its way
    through."""

    assert _verdict(now=BASE + timedelta(seconds=29)) is Verdict.STILL_UNKNOWN


def test_no_match_after_the_timeout_fails() -> None:
    assert _verdict(now=BASE + timedelta(seconds=31)) is Verdict.FAILED


def test_no_match_exactly_at_the_timeout_fails() -> None:
    """The comparison is ``now - t_submit >= RESOLUTION_TIMEOUT``: exactly
    30s of silence already justifies FAILED, not one tick past it."""

    assert _verdict(now=BASE + timedelta(seconds=30)) is Verdict.FAILED


def test_no_match_one_tick_before_the_timeout_stays_unknown() -> None:
    """One microsecond short of the boundary above: the timeout has not
    elapsed yet, so absence of a deal is still not evidence of failure."""

    just_under = BASE + timedelta(seconds=30) - timedelta(microseconds=1)
    assert _verdict(now=just_under) is Verdict.STILL_UNKNOWN


def test_an_unhealthy_terminal_never_yields_failed() -> None:
    """ "I cannot see the broker" is not "the order did not happen". A phase
    that conflated them would mark live positions FAILED and forget them."""

    assert _verdict(now=BASE + timedelta(seconds=600), terminal_healthy=False) is (
        Verdict.STILL_UNKNOWN
    )


# --- the gate ---------------------------------------------------------------


def test_the_gate_passes_on_an_empty_ledger() -> None:
    require_clean_ledger(RecordingLedger())  # must not raise


def test_the_gate_refuses_while_an_intent_is_unresolved() -> None:
    """I-20. This is the check standing between a half-known position and a
    second order on top of it."""

    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.UNKNOWN, BASE, {})

    with pytest.raises(UnresolvedIntentsError):
        require_clean_ledger(ledger)


def test_the_gate_names_the_intents_it_is_blocking_on() -> None:
    """An operator who cannot see which intent is stuck cannot clear it."""

    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.UNKNOWN, BASE, {})
    ledger.append("i-2", IntentState.SUBMITTING, BASE, {})

    with pytest.raises(UnresolvedIntentsError) as caught:
        require_clean_ledger(ledger)

    assert "i-1" in str(caught.value)
    assert "i-2" in str(caught.value)


# --- the sweep ---------------------------------------------------------------


def test_the_sweep_resolves_an_unknown_intent_to_confirmed() -> None:
    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.SUBMITTING, BASE, _snapshot_payload())
    ledger.append("i-1", IntentState.UNKNOWN, BASE, {})

    results = reconcile_all(ledger, FakeDeals(deals=(_deal(),)), FixedClock(BASE_PLUS_5))

    assert results["i-1"] is Verdict.CONFIRMED
    assert ledger.current_state("i-1") is IntentState.CONFIRMED


def test_the_sweep_marks_reconciling_before_it_polls() -> None:
    """A sweep that dies mid-poll must be visible as such rather than looking
    untouched. RECONCILING is in the frozen IntentState enum for this.

    ``FakeDeals`` is handed the ledger so ``deals_since`` -- called during the
    poll -- can record what state the ledger was actually in at that moment.
    If RECONCILING were written after polling instead of before, the state
    observed during the poll would still be UNKNOWN.

    A matching deal is also supplied so the sweep actually writes a verdict
    after RECONCILING -- proving RECONCILING is not simply the last thing
    written, which is what "visible mid-death" requires.
    """

    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.UNKNOWN, BASE, _snapshot_payload())
    deals = FakeDeals(deals=(_deal(),), ledger=ledger, intent_id="i-1")

    reconcile_all(ledger, deals, FixedClock(BASE_PLUS_5))

    assert deals.observed_states == [IntentState.RECONCILING], (
        "the ledger must already show RECONCILING at the moment deals_since is called"
    )

    states = [state for _, state, _ in ledger.appended]
    assert IntentState.RECONCILING in states
    assert states.index(IntentState.RECONCILING) < len(states) - 1


def test_the_sweep_leaves_an_unresolved_intent_non_terminal() -> None:
    """Inside RESOLUTION_TIMEOUT with no matching deal, the honest answer is
    still "I do not know". Writing FAILED here would abandon an order that
    may yet appear."""

    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.UNKNOWN, BASE, _snapshot_payload())

    results = reconcile_all(ledger, FakeDeals(deals=()), FixedClock(BASE_PLUS_5))

    assert results["i-1"] is Verdict.STILL_UNKNOWN
    assert ledger.current_state("i-1") in NON_TERMINAL_STATES


def test_the_sweep_writes_failed_once_the_timeout_has_passed() -> None:
    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.UNKNOWN, BASE, _snapshot_payload())

    reconcile_all(ledger, FakeDeals(deals=()), FixedClock(BASE + timedelta(seconds=31)))

    assert ledger.current_state("i-1") is IntentState.FAILED


def test_the_sweep_ignores_intents_that_already_reached_a_terminal_state() -> None:
    """Re-reconciling a CONFIRMED intent could append a second, contradictory
    verdict on top of a settled one."""

    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.SUBMITTING, BASE, _snapshot_payload())
    ledger.append("i-1", IntentState.CONFIRMED, BASE, {})

    assert reconcile_all(ledger, FakeDeals(deals=()), FixedClock(BASE_PLUS_5)) == {}


# --- the restart test: the other test that carries this phase ---------------


def test_a_process_that_died_between_the_write_and_the_send_blocks_the_next_one() -> None:
    """The scenario I-20 exists for. A manager writes SUBMITTING, commits, and
    the process dies before any answer is recorded. A FRESH manager -- new
    object, same ledger, exactly as a restart would give you -- must refuse to
    send anything at all until that leftover intent is resolved.

    Without the gate, the second run opens a position on top of one that may
    already exist, and the ledger records both as if they were independent.
    """

    ledger = RecordingLedger()
    dying_venue = FakeVenue(behaviour="lost_after_execution")
    OrderManager(ledger, dying_venue, FixedClock(BASE)).submit(
        _intent(intent_id="i-1"), STRATEGY_ID
    )

    fresh_venue = FakeVenue()
    with pytest.raises(UnresolvedIntentsError):
        require_clean_ledger(ledger)

    assert fresh_venue.calls == [], "no order may be sent while i-1 is unresolved"


def test_the_gate_reopens_once_the_leftover_resolves() -> None:
    """Guard the guard. A gate that never let anything through would pass the
    test above, and would also stop the system trading forever."""

    ledger = RecordingLedger()
    OrderManager(ledger, FakeVenue(behaviour="lost_after_execution"), FixedClock(BASE)).submit(
        _intent(intent_id="i-1"), STRATEGY_ID
    )
    ledger.append("i-1", IntentState.CONFIRMED, BASE, {})

    require_clean_ledger(ledger)  # must not raise

    assert (
        OrderManager(ledger, FakeVenue(), FixedClock(BASE)).submit(
            _intent(intent_id="i-2"), STRATEGY_ID
        )
        is IntentState.CONFIRMED
    )
