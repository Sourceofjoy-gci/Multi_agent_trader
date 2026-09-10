"""Tests for the position guard's daemon cycle.

``PositionGuard.cycle()`` joins Task 3's pure ``decide()`` to a broker and a
store: read the broker, read what we recorded, decide, act, record. The
property this whole task exists to guarantee -- I-21, a missing stop is
restored or escalated within two cycles -- is
``test_a_restore_that_keeps_failing_escalates_by_the_third_cycle``.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from tests.unit.execution.conftest import NOW, FakeProtectionVenue, RecordingPositionStore
from trading_house.core.clock import FixedClock
from trading_house.core.venue import PositionRecord
from trading_house.execution.guard import r_multiple
from trading_house.execution.loop import PositionGuard

OWNED_RANGES = ((110000, 110100),)


def _observed(**overrides: object) -> PositionRecord:
    fields: dict[str, object] = {
        "magic": 110042,
        "server_symbol": "EURUSD",
        "volume": Decimal("0.25"),
        "position_ticket": 7,
        "stop_loss": Decimal("1.09700"),
        "open_price": Decimal("1.10000"),
        "is_buy": True,
        "opened_at": NOW,
    }
    fields.update(overrides)
    return PositionRecord(**fields)  # type: ignore[arg-type]


class RecordingEscalator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def escalate(self, reason: str, payload: Mapping[str, Any]) -> None:
        self.calls.append((reason, payload))


def _default_store() -> RecordingPositionStore:
    """The store a test gets when it does not care to seed one itself:
    ticket 7, protected at the same stop ``_observed()`` defaults to."""

    store = RecordingPositionStore()
    store.append(7, "OPEN_PROTECTED", NOW, {"stop_loss": "1.09700"})
    return store


def _guard(
    venue: FakeProtectionVenue,
    store: RecordingPositionStore | None = None,
    *,
    escalator: RecordingEscalator | None = None,
) -> PositionGuard:
    return PositionGuard(
        store=store if store is not None else _default_store(),
        venue=venue,
        escalator=escalator if escalator is not None else RecordingEscalator(),
        clock=FixedClock(NOW),
        owned_magic_ranges=OWNED_RANGES,
        min_stop_distance=Decimal("0.00001"),
        default_stop_distance=Decimal("0.00300"),
    )


def test_an_unreadable_position_list_escalates_and_acts_on_nothing() -> None:
    """None means the broker read FAILED. Treating it as "no positions" would
    conclude every position had closed and stop guarding all of them."""

    venue = FakeProtectionVenue(positions=None)
    report = _guard(venue).cycle()

    assert report.escalated == 1
    assert venue.amended == []


def test_a_matching_stop_writes_nothing() -> None:
    store = RecordingPositionStore()
    store.append(7, "OPEN_PROTECTED", NOW, {"stop_loss": "1.09700"})

    _guard(FakeProtectionVenue(positions=[_observed(stop_loss=Decimal("1.09700"))]), store).cycle()

    assert len(store.appended) == 1  # only the seed


def test_a_missing_stop_is_restored_and_recorded() -> None:
    store = RecordingPositionStore()
    store.append(7, "OPEN_PROTECTED", NOW, {"stop_loss": "1.09700"})
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)])

    _guard(venue, store).cycle()

    assert venue.amended[0].stop_loss == Decimal("1.09700")
    assert store.appended[-1][1] == "OPEN_PROTECTED"


def test_a_restore_that_keeps_failing_escalates_by_the_third_cycle() -> None:
    """I-21: restored or escalated within two cycles."""

    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], amend_fails=True)
    guard = _guard(venue)

    guard.cycle()
    guard.cycle()
    report = guard.cycle()

    assert report.escalated == 1


def test_shutdown_is_recorded() -> None:
    """A gap in the event stream must be explainable. An unrecorded stop looks
    identical to a guard that silently died."""

    store, stop = RecordingPositionStore(), threading.Event()
    stop.set()

    _guard(FakeProtectionVenue(), store).run(stop, interval_seconds=0.0)

    assert store.appended[-1][1] == "GUARD_STOPPED"


def test_an_unavailable_price_still_protects_the_position() -> None:
    """R-multiples are analytics; the stop is the job. A guard that skipped
    restoring a missing stop because it could not read a quote would abandon a
    naked position over a number nobody trades on."""

    store = RecordingPositionStore()
    store.append(7, "OPEN_PROTECTED", NOW, {"stop_loss": "1.09700"})
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], closing_price_result=None)

    _guard(venue, store).cycle()

    assert venue.amended[0].stop_loss == Decimal("1.09700")


def test_a_position_outside_our_magic_ranges_is_left_completely_alone() -> None:
    """A manually-opened trade, or another book's. Amending its stop would be
    this system reaching into a position it knows nothing about -- and MT5
    tags an untagged trade with magic 0, so this is the common case, not the
    exotic one."""

    venue = FakeProtectionVenue(positions=[_observed(magic=0, stop_loss=None)])
    report = _guard(venue, RecordingPositionStore()).cycle()

    assert venue.amended == []
    assert report.checked == 0
    assert report.escalated == 0


def test_adopting_an_orphan_executes_before_it_escalates() -> None:
    """Spec 5.2's escalation ends with "stop attempting modifications" --
    escalating before adopting would leave the orphan naked, the opposite of
    D-5's intent. The orphan already carries a broker-side stop, so decide()
    adopts it as-is and marks escalate=True on the same GuardAction."""

    order: list[str] = []
    store = RecordingPositionStore()  # empty: ticket 7 is unrecorded, an orphan
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=Decimal("1.09700"))])
    escalator = RecordingEscalator()

    real_amend = venue.amend_protection

    def watched_amend(*args: Any, **kwargs: Any) -> Any:
        order.append("amend")
        return real_amend(*args, **kwargs)

    venue.amend_protection = watched_amend  # type: ignore[method-assign]

    real_escalate = escalator.escalate

    def watched_escalate(*args: Any, **kwargs: Any) -> None:
        order.append("escalate")
        real_escalate(*args, **kwargs)

    escalator.escalate = watched_escalate  # type: ignore[method-assign]

    report = _guard(venue, store, escalator=escalator).cycle()

    assert venue.amended[0].stop_loss == Decimal("1.09700")
    assert escalator.calls != []
    assert order == ["amend", "escalate"]
    assert report.escalated == 1


def test_r_multiple_is_signed_by_side_against_the_fixed_risk_distance() -> None:
    """Entry 1.10000, initial risk 0.00300. At 1.10300 a BUY is +1R and a SELL
    at the same price is -1R. Getting the sign wrong would report every losing
    short as a winner."""

    assert r_multiple(
        is_buy=True,
        open_price=Decimal("1.10000"),
        current=Decimal("1.10300"),
        initial_risk_distance=Decimal("0.00300"),
    ) == pytest.approx(1.0)
    assert r_multiple(
        is_buy=False,
        open_price=Decimal("1.10000"),
        current=Decimal("1.10300"),
        initial_risk_distance=Decimal("0.00300"),
    ) == pytest.approx(-1.0)


def test_mae_and_mfe_are_the_worst_and_best_ever_seen_not_the_latest() -> None:
    """The point of recording them: a position that reached +2R and came back
    to 0 must still report mfe_r 2.0. Storing the current excursion instead
    would pass every single-cycle test and destroy every downstream analysis."""

    store = RecordingPositionStore()
    store.append(
        7,
        "OPEN_PROTECTED",
        NOW,
        {"stop_loss": "1.09700", "open_price": "1.10000", "initial_risk_distance": "0.00300"},
    )
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=Decimal("1.09700"))])
    guard = _guard(venue, store)

    for price in ("1.10150", "1.10600", "1.09775", "1.10075"):
        venue.closing_price_result = Decimal(price)
        guard.cycle()

    final = store.appended[-1][2]
    assert float(final["mfe_r"]) == pytest.approx(2.0)  # from 1.10600
    assert float(final["mae_r"]) == pytest.approx(-0.75)  # from 1.09775


def test_the_risk_distance_is_never_recomputed_as_the_stop_moves() -> None:
    """Section 8.2 fixes initial_risk_distance at entry. Recomputing it from
    the CURRENT stop would silently redefine R every time the guard tightened
    one, making every trade's R-multiples incomparable with every other's."""

    store = RecordingPositionStore()
    store.append(
        7,
        "OPEN_PROTECTED",
        NOW,
        {
            "stop_loss": "1.09700",
            "open_price": "1.10000",
            "initial_risk_distance": "0.00300",
        },
    )
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=Decimal("1.09900"))])

    _guard(venue, store).cycle()

    assert store.appended[-1][2]["initial_risk_distance"] == "0.00300"
