"""Turns "we don't know what happened to that order" into a decision.

Two distinct durations matter here, and conflating them is the defect this
module exists to avoid. ``MATCH_LOOKBACK`` is how far *before* ``t_submit`` a
deal may be stamped and still be ours -- broker clock skew, not an
open-ended history. ``RESOLUTION_TIMEOUT`` is how long *after* ``t_submit``
the reconciler may give up and say FAILED. Both are module constants, never
arguments, so no caller can shorten the timeout.

``verdict()`` is the pure decision table. ``reconcile_all()`` is the sweep
that applies it to every non-terminal intent and writes the result back --
the single component the gate and the ``order reconcile`` command both
drive. ``require_clean_ledger`` is the gate itself: it runs that sweep and
then refuses to let a new order out if anything is still unresolved (I-20).

**Evidence is deals *and* open positions.** A deal is the ordinary proof an
order happened; a live position is the stronger one, because deal history can
be unreadable, delayed, or -- with a lost response -- never observed at all,
while a position sitting at the broker is the thing we would actually be
orphaning. So a position carrying our magic can confirm on its own, and can
never be outvoted into FAILED by a silent deal history.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from trading_house.core.clock import Clock
from trading_house.core.errors import UnresolvedIntentsError
from trading_house.core.values import IntentState
from trading_house.core.venue import DealRecord, PositionRecord
from trading_house.execution.ledger import IntentLedger

MATCH_LOOKBACK = timedelta(seconds=60)
RESOLUTION_TIMEOUT = timedelta(seconds=30)


class Verdict(str, Enum):  # noqa: UP042
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    STILL_UNKNOWN = "STILL_UNKNOWN"


@dataclass(frozen=True, slots=True)
class Resolution:
    """A verdict plus the evidence behind it.

    ``filled_quantity`` is what the broker actually did, which a partial fill
    makes smaller than the requested quantity (D-8) -- CONFIRMED is recorded
    at this number, never at the number we asked for.

    ``escalate`` is set only for the two genuinely ambiguous outcomes: more
    than one candidate, or a broker we cannot see. "The timeout simply has
    not elapsed yet" is not an escalation; marking gateway state stale every
    few seconds of normal latency would make staleness mean nothing.
    """

    verdict: Verdict
    escalate: bool = False
    position_ticket: int | None = None
    filled_quantity: Decimal | None = None
    matched_on: str | None = None


def verdict(
    *,
    magic: int,
    server_symbol: str,
    volume: Decimal,
    t_submit: datetime,
    deals: Sequence[DealRecord],
    positions: Sequence[PositionRecord],
    now: datetime,
    terminal_healthy: bool,
) -> Resolution:
    """Pure. No broker, no clock, no ledger -- so every hard case is a table.

    Order matters: the ambiguity check precedes the health check, because
    two matches is a definite ambiguity regardless of terminal health, and
    both precede the timeout, which is the only path to FAILED.
    """

    window_start = t_submit - MATCH_LOOKBACK
    our_positions = [position for position in positions if position.magic == magic]
    matching_positions = [
        position
        for position in our_positions
        if position.server_symbol == server_symbol and position.volume <= volume
    ]
    matching_deals = [
        deal
        for deal in deals
        if deal.magic == magic
        and deal.server_symbol == server_symbol
        # At or below, never above: a fill cannot exceed the request, but it
        # can fall short of it (D-8). Demanding equality here leaves every
        # partial fill unmatched until the timeout declares FAILED, with the
        # filled lots live at the broker and nothing looking for them.
        and deal.volume <= volume
        and window_start <= deal.dealt_at <= now
    ]

    # One position and the deal that opened it are one candidate, not two.
    # Counting rows instead of tickets would make the ordinary confirmed case
    # -- a fill, and the position it produced -- look exactly like the magic
    # collision D-7 refuses to guess at.
    tickets = {position.position_ticket for position in matching_positions} | {
        deal.position_ticket for deal in matching_deals
    }
    if len(tickets) == 1:
        return _confirmed(next(iter(tickets)), matching_positions, matching_deals)
    if tickets:
        # This is what a magic collision actually produces. Adopting one
        # candidate would attach our ledger to a position that may not be ours.
        return Resolution(Verdict.STILL_UNKNOWN, escalate=True)
    if not terminal_healthy:
        # "I cannot see the broker" is not "the order did not happen".
        return Resolution(Verdict.STILL_UNKNOWN, escalate=True)
    if our_positions:
        # A live position carrying our magic that did not match on symbol or
        # volume is still proof that something of ours is open. Whatever it
        # is, it is not FAILED, and a human needs to look at it.
        return Resolution(Verdict.STILL_UNKNOWN, escalate=True)
    if now - t_submit >= RESOLUTION_TIMEOUT:
        return Resolution(Verdict.FAILED)
    return Resolution(Verdict.STILL_UNKNOWN)


def _confirmed(
    ticket: int,
    positions: Sequence[PositionRecord],
    deals: Sequence[DealRecord],
) -> Resolution:
    """Confirm at the volume actually filled.

    The open position is preferred over the deals when both are present: it
    is the live truth, and it is already the sum of however many deals built
    it. With deals alone, their volumes are summed, because a fill split
    across price levels is several deals against one position.
    """

    position = next((p for p in positions if p.position_ticket == ticket), None)
    if position is not None:
        return Resolution(
            Verdict.CONFIRMED,
            position_ticket=ticket,
            filled_quantity=position.volume,
            matched_on="position",
        )
    filled = sum((deal.volume for deal in deals if deal.position_ticket == ticket), Decimal(0))
    return Resolution(
        Verdict.CONFIRMED,
        position_ticket=ticket,
        filled_quantity=filled,
        matched_on="deal",
    )


class DealSource(Protocol):
    """``execution/``'s own port onto broker deals and positions, satisfied
    structurally by the MT5 adapter -- ``execution/`` must not import
    ``brokers/``.

    Both reads return ``None`` for "could not read", which the sweep treats
    exactly as it treats an unhealthy terminal.
    """

    def deals_since(self, start: datetime) -> Sequence[DealRecord] | None: ...

    def positions_now(self) -> Sequence[PositionRecord] | None: ...

    def terminal_healthy(self) -> bool: ...


def _match_fields(payload: Mapping[str, Any]) -> tuple[int, str]:
    """Magic and server symbol out of a SUBMITTING snapshot payload.

    ``venue_ref`` is ``None`` when the caller never populated it before
    calling ``submit()``. There is then no ticket to correlate a deal
    against, so a sentinel that can never match a real deal is returned --
    the intent still resolves correctly via the timeout path.
    """

    venue_ref = payload.get("venue_ref")
    if venue_ref is None:
        return -1, ""
    return venue_ref["magic"], venue_ref["server_symbol"]


def _confirmed_payload(requested: Decimal, resolution: Resolution) -> dict[str, Any]:
    """What reconciliation concluded, as JSON-safe values. Every ``Decimal``
    is a string; a float in JSONB is silent corruption of the money path."""

    filled = resolution.filled_quantity
    return {
        "requested_quantity": str(requested),
        "filled_quantity": None if filled is None else str(filled),
        "partial_fill": filled is not None and filled < requested,
        "matched_on": resolution.matched_on,
        "position_ticket": resolution.position_ticket,
    }


def reconcile_all(
    ledger: IntentLedger,
    deals: DealSource,
    clock: Clock,
    escalate: Callable[[], None] | None = None,
) -> Mapping[str, Verdict]:
    """Read every non-terminal intent, poll the venue, write back a verdict.

    Writes RECONCILING *before* polling so a sweep that dies mid-poll is
    visible as such. Appends nothing further on STILL_UNKNOWN -- the
    RECONCILING event already records that an attempt was made -- but calls
    ``escalate`` (in practice ``Gateway.mark_stale``) when that STILL_UNKNOWN
    was ambiguous rather than merely early, which is what spec 6 means by
    escalation.
    """

    results: dict[str, Verdict] = {}
    for intent_id in ledger.non_terminal():
        # The intent's very first event is always its SUBMITTING snapshot --
        # the only payload an UNKNOWN intent ever gets.
        payload = ledger.events_for(intent_id)[0].payload
        now = clock.now()
        ledger.append(intent_id, IntentState.RECONCILING, now, {})

        magic, server_symbol = _match_fields(payload)
        requested = Decimal(payload["quantity"])
        t_submit = datetime.fromisoformat(payload["t_submit_utc"])

        candidate_deals = deals.deals_since(t_submit - MATCH_LOOKBACK)
        open_positions = deals.positions_now()
        # A read that failed is exactly as blind as a disconnected terminal.
        # Letting an unreadable history fall through as "no deals" is how a
        # live position gets marked FAILED and then forgotten.
        healthy = (
            deals.terminal_healthy() and candidate_deals is not None and open_positions is not None
        )
        resolution = verdict(
            magic=magic,
            server_symbol=server_symbol,
            volume=requested,
            t_submit=t_submit,
            deals=candidate_deals or (),
            positions=open_positions or (),
            now=now,
            terminal_healthy=healthy,
        )
        results[intent_id] = resolution.verdict

        if resolution.verdict is Verdict.CONFIRMED:
            ledger.append(
                intent_id, IntentState.CONFIRMED, now, _confirmed_payload(requested, resolution)
            )
        elif resolution.verdict is Verdict.FAILED:
            ledger.append(intent_id, IntentState.FAILED, now, {})
        elif resolution.escalate and escalate is not None:
            escalate()

    return results


def require_clean_ledger(
    ledger: IntentLedger,
    deals: DealSource,
    clock: Clock,
    escalate: Callable[[], None] | None = None,
) -> None:
    """The check standing between a half-known position and a second order on
    top of it (I-20).

    Runs the sweep first (spec 6.1, D-3) so a restart self-heals instead of
    waiting for an operator to type ``order reconcile``, then refuses on
    whatever the sweep could not resolve. Fails closed: an intent the sweep
    left non-terminal still blocks.
    """

    reconcile_all(ledger, deals, clock, escalate)
    unresolved = ledger.non_terminal()
    if unresolved:
        raise UnresolvedIntentsError(unresolved)
