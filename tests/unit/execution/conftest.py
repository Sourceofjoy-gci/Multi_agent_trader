"""A venue that can do what a real broker cannot on demand.

The property this phase exists to guarantee -- that a lost response never
doubles a position -- cannot be staged against a live broker, because you
cannot make MT5 return None *after* it has executed. That is why this fake is
the primary instrument and not a convenience.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_house.core.schemas import OrderIntent
from trading_house.core.values import IntentState, PositiveQuantity
from trading_house.core.venue import ExecutionOutcome, Mt5VenueRef, RejectReason, Venue
from trading_house.execution.ledger import NON_TERMINAL_STATES, IntentEvent


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
            raise TimeoutError("no response")
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
