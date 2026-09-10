"""The guard's pure decision function.

Given one broker-observed position and what we believe about it, ``decide()``
says what to do -- no broker, no clock, no database. It imports only from
``core/``: the ATR-derived stop distance arrives as a ``Decimal`` argument,
computed by the caller, not from ``features/``.

Branch order is load-bearing: a vanished position is checked first, then an
orphan (no record of it at all), then a missing stop, then a disagreement,
then nothing. Each of those is a case the next one would silently swallow if
it ran first -- see ``tests/unit/execution/test_guard.py`` for the mutation
that proves it.

``decide()`` must never return ``TIGHTEN_STOP`` or ``RESTORE_STOP`` with a
stop worse than ``recorded_stop`` -- I-8, a stop may never move away from
profit. ``decide_tighten()`` is the one place that rule for a trailing
candidate lives; the second enforcement layer is in
``brokers/mt5/adapter.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from trading_house.core.venue import PositionRecord

MAX_FAILED_RESTORES = 2


class ActionKind(str, Enum):  # noqa: UP042
    NOTHING = "nothing"
    RECORD_ONLY = "record_only"  # the broker disagrees; believe the broker
    RESTORE_STOP = "restore_stop"  # sl is missing; put it back
    TIGHTEN_STOP = "tighten_stop"
    ADOPT_ORPHAN = "adopt_orphan"  # a position this system has no record of at all
    RECORD_CLOSED = "record_closed"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class GuardAction:
    """``escalate`` is a signal, not an action: when True, ``kind`` is still
    executed FIRST and spec 5.2's escalation (``mark_stale()`` + an audit
    append + the position event + no further modification attempts) follows
    AFTER, never before. Both ``ADOPT_ORPHAN`` returns set ``escalate=True``:
    writing the adopted stop is itself a modification, so escalating first
    would leave the orphan naked in the gap, the opposite of D-5's intent.
    ``decide()`` only emits the signal -- it performs no I/O itself."""

    kind: ActionKind
    stop_loss: Decimal | None = None  # the stop to write, for the three that write one
    escalate: bool = False  # execute `kind`, THEN escalate per spec 5.2
    reason: str = ""


def decide(
    *,
    observed: PositionRecord | None,
    is_recorded: bool,
    is_buy: bool,
    recorded_stop: Decimal | None,
    min_stop_distance: Decimal,
    default_stop_distance: Decimal | None,
    failed_restores: int,
) -> GuardAction:
    if observed is None:
        if is_recorded:
            return GuardAction(ActionKind.RECORD_CLOSED, reason="position no longer at the broker")
        return GuardAction(ActionKind.NOTHING, reason="neither the broker nor we have it")

    if not is_recorded:
        if observed.stop_loss is not None:
            return GuardAction(
                ActionKind.ADOPT_ORPHAN,
                stop_loss=observed.stop_loss,
                escalate=True,
                reason="orphan retains its broker stop; adopted, then escalate per D-5",
            )
        if default_stop_distance is None:
            return GuardAction(ActionKind.ESCALATE, reason="orphan has no stop and no distance")
        distance = max(default_stop_distance, min_stop_distance)
        # An orphan has no record, so is_buy is only ever a caller's guess --
        # observed.is_buy is the broker's own answer and is already in hand.
        stop = observed.open_price - distance if observed.is_buy else observed.open_price + distance
        return GuardAction(
            ActionKind.ADOPT_ORPHAN,
            stop_loss=stop,
            escalate=True,
            reason="orphan adopted at supplied distance; adopted, then escalate per D-5",
        )

    if observed.stop_loss is None:
        if recorded_stop is None:
            return GuardAction(ActionKind.ESCALATE, reason="no stop anywhere to restore")
        if failed_restores >= MAX_FAILED_RESTORES:
            return GuardAction(ActionKind.ESCALATE, reason="restore already failed twice")
        return GuardAction(
            ActionKind.RESTORE_STOP, stop_loss=recorded_stop, reason="stop missing at the broker"
        )

    if observed.stop_loss != recorded_stop:
        return GuardAction(
            ActionKind.RECORD_ONLY,
            stop_loss=observed.stop_loss,
            reason="broker stop disagrees with the record",
        )

    return GuardAction(ActionKind.NOTHING, reason="stop matches the record")


def decide_tighten(*, is_buy: bool, current: Decimal, candidate: Decimal) -> GuardAction:
    """The narrow entry point for a trailing candidate -- the one home for
    I-8's no-widening rule so it is not scattered across callers."""

    improves = candidate > current if is_buy else candidate < current
    if improves:
        return GuardAction(ActionKind.TIGHTEN_STOP, stop_loss=candidate, reason="stop tightened")
    return GuardAction(ActionKind.NOTHING, reason="candidate would not tighten the stop")


def r_multiple(
    *, is_buy: bool, open_price: Decimal, current: Decimal, initial_risk_distance: Decimal
) -> float:
    """The signed R-multiple of ``current`` against the fixed entry-to-stop
    distance established at open (Section 8.2). ``float`` is deliberate --
    analytics, not money, unlike every price and distance here, which stay
    ``Decimal`` all the way through this computation.
    """

    excursion = (current - open_price) if is_buy else (open_price - current)
    return float(excursion / initial_risk_distance)
