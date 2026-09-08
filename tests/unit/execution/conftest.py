"""A venue that can do what a real broker cannot on demand.

The property this phase exists to guarantee -- that a lost response never
doubles a position -- cannot be staged against a live broker, because you
cannot make MT5 return None *after* it has executed. That is why this fake is
the primary instrument and not a convenience.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading_house.core.schemas import OrderIntent, Side
from trading_house.core.values import IntentState, PositiveQuantity, TimeInForce
from trading_house.core.venue import DealRecord, ExecutionOutcome, Mt5VenueRef, RejectReason, Venue
from trading_house.execution.ledger import NON_TERMINAL_STATES, IntentEvent

NOW = datetime(2026, 8, 25, tzinfo=UTC)
BASE = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _intent(
    *,
    intent_id: str = "intent-1",
    quantity: Decimal = Decimal("0.10"),
    venue_ref: Mt5VenueRef | None = None,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
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
        venue_ref=venue_ref,
        outcome=None,
    )


def _snapshot_payload(**overrides: Any) -> dict[str, Any]:
    """A SUBMITTING payload shaped exactly like ``OrderManager``'s own
    snapshot -- same keys, every ``Decimal`` a string -- so the reconciliation
    sweep tests read back real submit-time data, not a shortcut shape."""

    fields: dict[str, Any] = {
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "intent_id": "i-1",
        "max_slippage_bps": "2",
        "proposal_id": "proposal-1",
        "quantity": "0.25",
        "quantity_unit": "lots",
        "side": Side.BUY.value,
        "stop_loss": "1.0950",
        "take_profit": None,
        "time_in_force": TimeInForce.IOC.value,
        "venue_ref": {
            "venue": Venue.MT5.value,
            "magic": 110042,
            "server_symbol": "EURUSD",
            "order_ticket": None,
            "position_ticket": None,
            "retcode": None,
        },
        "t_submit_utc": BASE.isoformat(),
    }
    fields.update(overrides)
    return fields


class FakeVenue:
    def __init__(self, *, behaviour: str = "accept") -> None:
        self.behaviour = behaviour
        self.calls: list[OrderIntent] = []
        self.executed: list[OrderIntent] = []

    def submit(self, intent: OrderIntent) -> ExecutionOutcome:
        self.calls.append(intent)
        if self.behaviour == "lost_after_execution":
            # The broker DID execute; the answer never came back.
            self.executed.append(intent)
            raise TimeoutError("no response")
        if self.behaviour == "lost_before_execution":
            # Looks like "the request never left the box" -- a ConnectionError
            # raised while reading the response looks identical. Nothing in
            # the exception says which happened, so this must be treated
            # exactly like lost_after_execution: UNKNOWN, never resent.
            raise ConnectionError("no response")
        if self.behaviour == "reject":
            return ExecutionOutcome(
                accepted=False,
                venue_ref=None,
                filled_quantity=None,
                fill_price=None,
                reject_reason=RejectReason.INSUFFICIENT_FUNDS,
            )
        if self.behaviour == "partial":
            self.executed.append(intent)
            return ExecutionOutcome(
                accepted=True,
                venue_ref=Mt5VenueRef(
                    venue=Venue.MT5,
                    magic=110042,
                    server_symbol="EURUSD",
                    order_ticket=1,
                    position_ticket=2,
                    retcode=10010,
                ),
                filled_quantity=PositiveQuantity(
                    amount=intent.quantity.amount / 2, unit=intent.quantity.unit
                ),
                fill_price=Decimal("1.10000"),
                reject_reason=None,
            )
        self.executed.append(intent)
        return ExecutionOutcome(
            accepted=True,
            venue_ref=Mt5VenueRef(
                venue=Venue.MT5,
                magic=110042,
                server_symbol="EURUSD",
                order_ticket=1,
                position_ticket=2,
                retcode=10009,
            ),
            filled_quantity=intent.quantity,
            fill_price=Decimal("1.10000"),
            reject_reason=None,
        )


class RecordingLedger:
    """An in-memory ``IntentLedger`` that remembers every append, in order."""

    def __init__(self, *, on_append: Callable[[IntentState], None] | None = None) -> None:
        self._on_append = on_append
        self.appended: list[tuple[str, IntentState, Mapping[str, Any]]] = []
        self._events: dict[str, list[IntentEvent]] = {}

    def append(
        self,
        intent_id: str,
        state: IntentState,
        event_time: datetime,
        payload: Mapping[str, Any],
    ) -> None:
        if self._on_append is not None:
            self._on_append(state)
        self.appended.append((intent_id, state, payload))
        events = self._events.setdefault(intent_id, [])
        events.append(
            IntentEvent(
                seq=len(events) + 1,
                intent_id=intent_id,
                state=state,
                event_time=event_time,
                payload=payload,
            )
        )

    def current_state(self, intent_id: str) -> IntentState | None:
        events = self._events.get(intent_id)
        return events[-1].state if events else None

    def events_for(self, intent_id: str) -> tuple[IntentEvent, ...]:
        return tuple(self._events.get(intent_id, ()))

    def non_terminal(self) -> tuple[str, ...]:
        return tuple(
            intent_id
            for intent_id, events in self._events.items()
            if events[-1].state in NON_TERMINAL_STATES
        )


class FakeDeals:
    """A ``DealSource`` double: a fixed set of deals and a fixed terminal
    health, so reconciliation tests control both without a broker.

    ``ledger`` and ``intent_id`` are optional: when given, ``deals_since``
    records ``ledger.current_state(intent_id)`` into ``observed_states`` at
    the moment it is called -- the only way a test can see what state the
    ledger was in *during* the poll, since this stub otherwise has no view
    of ledger state at all.
    """

    def __init__(
        self,
        *,
        deals: tuple[DealRecord, ...] = (),
        healthy: bool = True,
        ledger: RecordingLedger | None = None,
        intent_id: str | None = None,
    ) -> None:
        self.deals = deals
        self.healthy = healthy
        self._ledger = ledger
        self._intent_id = intent_id
        self.observed_states: list[IntentState | None] = []

    def deals_since(self, start: datetime) -> tuple[DealRecord, ...]:
        ledger, intent_id = self._ledger, self._intent_id
        if ledger is not None and intent_id is not None:
            self.observed_states.append(ledger.current_state(intent_id))
        return self.deals

    def terminal_healthy(self) -> bool:
        return self.healthy
