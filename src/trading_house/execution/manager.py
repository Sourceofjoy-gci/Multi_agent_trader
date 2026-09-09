"""Writes, sends, records. Never reconciles, never resends.

MT5 has no client order ID: a timed-out send is genuinely ambiguous between
"lost on the way out" (nothing happened) and "lost on the way back" (the
order executed and the confirmation never arrived). Retrying resolves the
first case and doubles a real position in the second, and nothing in the
response distinguishes them. So this manager writes SUBMITTING durably
*before* it ever calls the venue, sends exactly once, records whatever it
learns -- including nothing at all, on a lost response -- and stops.
Reconciling an UNKNOWN intent back to a terminal state is a separate
concern, handled by the same recovery path that runs after a crash, not by
a retry loop here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from trading_house.core.clock import Clock
from trading_house.core.errors import IntentAlreadySubmittedError
from trading_house.core.schemas import OrderIntent
from trading_house.core.values import IntentState
from trading_house.core.venue import ExecutionOutcome, recovery_for
from trading_house.execution.ledger import IntentLedger


class VenueSubmitPort(Protocol):
    """The one thing execution/ needs from a broker: send it, once."""

    def submit(self, intent: OrderIntent) -> ExecutionOutcome: ...


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _snapshot(intent: OrderIntent, strategy_id: str) -> dict[str, Any]:
    """The intent's own fields, plus ``strategy_id`` -- which ``OrderIntent``
    does not carry -- so that a later reconcile can rebuild a
    ``PositionState`` from this payload alone. Every ``Decimal`` is recorded
    as a string; a float in JSONB is silent corruption of the money path.

    ``venue_ref`` and ``t_submit_utc`` are recorded here -- not derived --
    because reconciliation (Task 4) has no other source for magic, server
    symbol and submit time on an UNKNOWN intent: there is no CONFIRMED
    payload to fall back on, and execution/ may not import ``derive_magic``
    from brokers/. The caller populates ``intent.venue_ref`` before calling
    ``submit()``; a value derived a second time here is the same defect as
    deriving it twice anywhere else.
    """

    return {
        "intent_id": intent.intent_id,
        "proposal_id": intent.proposal_id,
        "strategy_id": strategy_id,
        "book": intent.book,
        "instrument_id": intent.instrument_id,
        "side": intent.side.value,
        "quantity": str(intent.quantity.amount),
        "quantity_unit": intent.quantity.unit,
        "stop_loss": str(intent.stop_loss),
        "take_profit": _decimal_str(intent.take_profit),
        "time_in_force": intent.time_in_force.value,
        "max_slippage_bps": str(intent.max_slippage_bps),
        "venue_ref": intent.venue_ref.model_dump(mode="json")
        if intent.venue_ref is not None
        else None,
        "t_submit_utc": intent.t_submit_utc.isoformat(),
    }


def _outcome_payload(intent: OrderIntent, outcome: ExecutionOutcome) -> dict[str, Any]:
    """What the venue actually did, as JSON-safe values. A partial fill is
    recorded at the quantity the broker actually filled -- the requested
    quantity is kept alongside it, never in its place.
    """

    filled = outcome.filled_quantity
    payload: dict[str, Any] = {
        "accepted": outcome.accepted,
        "requested_quantity": str(intent.quantity.amount),
        "filled_quantity": _decimal_str(filled.amount if filled is not None else None),
        "fill_price": _decimal_str(outcome.fill_price),
    }
    if outcome.venue_ref is not None:
        payload["venue_ref"] = outcome.venue_ref.model_dump(mode="json")
    if not outcome.accepted and outcome.reject_reason is not None:
        payload["reject_reason"] = outcome.reject_reason.value
        payload["recovery"] = recovery_for(outcome.reject_reason).value
    return payload


class OrderManager:
    """Writes, sends, records. Never reconciles, never resends."""

    def __init__(self, ledger: IntentLedger, venue: VenueSubmitPort, clock: Clock) -> None:
        self._ledger = ledger
        self._venue = venue
        self._clock = clock

    def submit(self, intent: OrderIntent, strategy_id: str) -> IntentState:
        # Refuse before writing anything. If this intent_id has any ledger
        # event at all -- SUBMITTING, terminal, whatever -- a second submit()
        # is the resend §3.6 forbids, not a retry. A genuine retry mints a
        # fresh intent_id and starts over.
        if self._ledger.current_state(intent.intent_id) is not None:
            raise IntentAlreadySubmittedError()
        now = self._clock.now()
        self._ledger.append(
            intent.intent_id, IntentState.SUBMITTING, now, _snapshot(intent, strategy_id)
        )
        try:
            outcome = self._venue.submit(intent)
        except Exception:
            # Deliberately broad: ANY failure to obtain a usable answer is
            # UNKNOWN. Narrowing this to the exceptions we predicted would let
            # an unforeseen one escape, and an escaping exception leaves the
            # ledger saying SUBMITTING with nobody recording why.
            self._ledger.append(intent.intent_id, IntentState.UNKNOWN, self._clock.now(), {})
            return IntentState.UNKNOWN
        state = IntentState.CONFIRMED if outcome.accepted else IntentState.REJECTED
        self._ledger.append(
            intent.intent_id, state, self._clock.now(), _outcome_payload(intent, outcome)
        )
        return state
