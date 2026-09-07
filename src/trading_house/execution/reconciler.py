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
drive. ``require_clean_ledger`` is the gate itself: it refuses to let a new
order out while any earlier intent is still unresolved (I-20).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from trading_house.core.clock import Clock
from trading_house.core.errors import UnresolvedIntentsError
from trading_house.core.values import IntentState
from trading_house.core.venue import DealRecord
from trading_house.execution.ledger import IntentLedger

MATCH_LOOKBACK = timedelta(seconds=60)
RESOLUTION_TIMEOUT = timedelta(seconds=30)


class Verdict(str, Enum):  # noqa: UP042
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    STILL_UNKNOWN = "STILL_UNKNOWN"


def verdict(
    *,
    magic: int,
    server_symbol: str,
    volume: Decimal,
    t_submit: datetime,
    deals: Sequence[DealRecord],
    now: datetime,
    terminal_healthy: bool,
) -> Verdict:
    """Pure. No broker, no clock, no ledger -- so every hard case is a table.

    Order matters: the ambiguity check precedes the health check, because
    two matches is a definite ambiguity regardless of terminal health.
    """

    window_start = t_submit - MATCH_LOOKBACK
    matches = [
        deal
        for deal in deals
        if deal.magic == magic
        and deal.server_symbol == server_symbol
        and deal.volume == volume
        and window_start <= deal.dealt_at <= now
    ]
    if len(matches) == 1:
        return Verdict.CONFIRMED
    if len(matches) > 1:
        # This is what a magic collision actually produces. Adopting one
        # deal would attach our ledger to a position that may not be ours.
        return Verdict.STILL_UNKNOWN
    if not terminal_healthy:
        # "I cannot see the broker" is not "the order did not happen".
        return Verdict.STILL_UNKNOWN
    if now - t_submit >= RESOLUTION_TIMEOUT:
        return Verdict.FAILED
    return Verdict.STILL_UNKNOWN


def require_clean_ledger(ledger: IntentLedger) -> None:
    """The check standing between a half-known position and a second order
    on top of it (I-20). Refuses whenever any intent is still non-terminal."""

    unresolved = ledger.non_terminal()
    if unresolved:
        raise UnresolvedIntentsError(unresolved)


class DealSource(Protocol):
    """``execution/``'s own port onto broker deals, satisfied structurally
    by the MT5 adapter -- ``execution/`` must not import ``brokers/``."""

    def deals_since(self, start: datetime) -> Sequence[DealRecord]: ...

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


def reconcile_all(ledger: IntentLedger, deals: DealSource, clock: Clock) -> Mapping[str, Verdict]:
    """Read every non-terminal intent, poll the venue, write back a verdict.

    Writes RECONCILING *before* polling so a sweep that dies mid-poll is
    visible as such. Appends nothing further on STILL_UNKNOWN -- the
    RECONCILING event already records that an attempt was made.
    """

    results: dict[str, Verdict] = {}
    for intent_id in ledger.non_terminal():
        # The intent's very first event is always its SUBMITTING snapshot --
        # the only payload an UNKNOWN intent ever gets.
        payload = ledger.events_for(intent_id)[0].payload
        now = clock.now()
        ledger.append(intent_id, IntentState.RECONCILING, now, {})

        magic, server_symbol = _match_fields(payload)
        volume = Decimal(payload["quantity"])
        t_submit = datetime.fromisoformat(payload["t_submit_utc"])

        candidate_deals = deals.deals_since(t_submit - MATCH_LOOKBACK)
        result = verdict(
            magic=magic,
            server_symbol=server_symbol,
            volume=volume,
            t_submit=t_submit,
            deals=candidate_deals,
            now=now,
            terminal_healthy=deals.terminal_healthy(),
        )
        results[intent_id] = result

        if result is Verdict.CONFIRMED:
            ledger.append(intent_id, IntentState.CONFIRMED, now, {})
        elif result is Verdict.FAILED:
            ledger.append(intent_id, IntentState.FAILED, now, {})

    return results
