"""Tests for the guard's pure decision function.

``decide()`` takes one broker-observed position, what we believe about it, and
decides what to do -- no broker, no clock, no database. ``decide_tighten()``
is the narrow entry point for a trailing candidate, so the no-widening rule
(I-8) has one home instead of being scattered.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.unit.execution.conftest import NOW
from trading_house.core.venue import PositionRecord
from trading_house.execution.guard import ActionKind, GuardAction, decide, decide_tighten

BUY = True
STOP = Decimal("1.09700")


def _observed(**overrides: object) -> PositionRecord:
    fields: dict[str, object] = {
        "magic": 110042,
        "server_symbol": "EURUSD",
        "volume": Decimal("0.25"),
        "position_ticket": 7,
        "stop_loss": STOP,
        "open_price": Decimal("1.10000"),
        "is_buy": True,
        "opened_at": NOW,
    }
    fields.update(overrides)
    return PositionRecord(**fields)  # type: ignore[arg-type]


def _decide(**overrides: object) -> GuardAction:
    args: dict[str, object] = {
        "observed": _observed(),
        "is_recorded": True,
        "is_buy": BUY,
        "recorded_stop": STOP,
        "min_stop_distance": Decimal("0.00001"),
        "default_stop_distance": Decimal("0.00300"),
        "failed_restores": 0,
    }
    args.update(overrides)
    return decide(**args)  # type: ignore[arg-type]


def test_a_matching_stop_does_nothing_and_writes_nothing() -> None:
    """The overwhelmingly common cycle. If this wrote a row, the table would
    grow by 86,400 rows per position per day and bury every real event."""

    assert _decide().kind is ActionKind.NOTHING


def test_a_missing_stop_is_restored_to_the_recorded_one() -> None:
    """sl == 0 at the broker means unprotected. This is the case the guard
    exists for."""

    action = _decide(observed=_observed(stop_loss=None))

    assert action.kind is ActionKind.RESTORE_STOP
    assert action.stop_loss == STOP


def test_the_first_failed_restore_still_retries() -> None:
    """D-7 escalates on the SECOND failure. Escalating on the first throws
    away the retry that usually succeeds, and escalation means we stop
    attempting modifications entirely."""

    action = _decide(observed=_observed(stop_loss=None), failed_restores=1)

    assert action.kind is ActionKind.RESTORE_STOP


def test_a_second_failed_restore_escalates_instead_of_looping() -> None:
    """Retrying forever against a broker that keeps refusing is how a guard
    spins while a position sits unprotected."""

    action = _decide(observed=_observed(stop_loss=None), failed_restores=2)

    assert action.kind is ActionKind.ESCALATE


def test_a_broker_stop_that_disagrees_is_recorded_not_overwritten() -> None:
    """The broker is the truth about where the stop actually is. Overwriting it
    with our record would move a live stop based on stale belief."""

    action = _decide(observed=_observed(stop_loss=Decimal("1.09750")))

    assert action.kind is ActionKind.RECORD_ONLY
    assert action.stop_loss == Decimal("1.09750")


def test_a_broker_stop_that_is_wider_than_the_record_is_also_just_recorded() -> None:
    """The spec's rule is "believe the broker", not "believe the better stop".
    Reading I-8 as "push our tighter stop back" is a defensible misreading and
    the wrong one: the guard would be moving a live stop on stale belief, which
    is the thing RECORD_ONLY exists to prevent. Restoring is for a MISSING
    stop, not a disagreeing one."""

    action = _decide(observed=_observed(stop_loss=Decimal("1.09600")))

    assert action.kind is ActionKind.RECORD_ONLY
    assert action.stop_loss == Decimal("1.09600")


def test_an_orphan_with_a_stop_is_adopted_at_that_stop() -> None:
    action = _decide(is_recorded=False, recorded_stop=None)

    assert action.kind is ActionKind.ADOPT_ORPHAN
    assert action.stop_loss == STOP


def test_an_orphan_without_a_stop_is_adopted_at_the_supplied_distance() -> None:
    """The distance comes from the book's signed k_sigma against current ATR,
    computed by the caller. decide() never invents one."""

    action = _decide(observed=_observed(stop_loss=None), is_recorded=False, recorded_stop=None)

    assert action.kind is ActionKind.ADOPT_ORPHAN
    assert action.stop_loss == Decimal("1.09700")  # 1.10000 - 0.00300


def test_an_orphan_sell_is_adopted_at_a_stop_above_its_entry() -> None:
    """A sell's protective stop sits ABOVE entry. Subtracting the distance
    unconditionally -- which passes the buy case -- would place it below, where
    it is already breached the moment it is written."""

    action = _decide(
        observed=_observed(stop_loss=None, is_buy=False),
        is_buy=False,
        is_recorded=False,
        recorded_stop=None,
    )

    assert action.kind is ActionKind.ADOPT_ORPHAN
    assert action.stop_loss == Decimal("1.10300")  # 1.10000 + 0.00300


def test_the_orphan_stop_is_floored_by_the_broker_minimum() -> None:
    """Spec D-5. A distance tighter than the broker will accept is not a
    tighter stop, it is a rejected request -- and the position stays naked
    while we retry it."""

    action = _decide(
        observed=_observed(stop_loss=None),
        is_recorded=False,
        recorded_stop=None,
        default_stop_distance=Decimal("0.00100"),
        min_stop_distance=Decimal("0.00250"),
    )

    assert action.stop_loss == Decimal("1.09750")  # floored, not 1.09900


def test_an_orphan_without_a_stop_or_a_distance_escalates() -> None:
    """No ATR means no defensible distance. Inventing one would be exactly the
    fabrication this project refuses everywhere else."""

    action = _decide(
        observed=_observed(stop_loss=None),
        is_recorded=False,
        recorded_stop=None,
        default_stop_distance=None,
    )

    assert action.kind is ActionKind.ESCALATE


def test_a_recorded_position_with_no_stop_anywhere_escalates() -> None:
    """Reachable via an orphan that escalated without a stop: the next cycle
    sees it as recorded (an event was appended), still with no stop on the
    broker or in the record. There is nothing to restore to, and the guard
    already knows about this position, so this is not a fresh orphan."""

    action = _decide(observed=_observed(stop_loss=None), is_recorded=True, recorded_stop=None)

    assert action.kind is ActionKind.ESCALATE


def test_a_vanished_position_is_recorded_closed() -> None:
    assert _decide(observed=None).kind is ActionKind.RECORD_CLOSED


def test_a_vanished_position_with_no_record_does_nothing() -> None:
    """Neither the broker nor we have it. There is nothing to close."""

    assert _decide(observed=None, is_recorded=False).kind is ActionKind.NOTHING


@pytest.mark.parametrize(
    ("is_buy", "current", "candidate", "expected"),
    [
        (True, "1.09700", "1.09800", ActionKind.TIGHTEN_STOP),  # rises: allowed
        (True, "1.09700", "1.09600", ActionKind.NOTHING),  # falls: refused
        (False, "1.10300", "1.10200", ActionKind.TIGHTEN_STOP),  # falls: allowed
        (False, "1.10300", "1.10400", ActionKind.NOTHING),  # rises: refused
    ],
)
def test_a_stop_only_ever_moves_toward_profit(
    is_buy: bool, current: str, candidate: str, expected: ActionKind
) -> None:
    """The first of the two enforcement layers. A candidate that would widen
    produces NOTHING -- not an error, because a retraced price legitimately
    produces one every cycle."""

    action = decide_tighten(is_buy=is_buy, current=Decimal(current), candidate=Decimal(candidate))

    assert action.kind is expected
    if expected is ActionKind.TIGHTEN_STOP:
        assert action.stop_loss == Decimal(candidate)
    else:
        assert action.stop_loss is None
