"""Tests for the position guard's daemon cycle.

``PositionGuard.cycle()`` joins Task 3's pure ``decide()`` to a broker and a
store: read the broker, read what we recorded, decide, act, record. The
property this whole task exists to guarantee -- I-21, a missing stop is
restored or escalated within two cycles -- is
``test_a_restore_that_keeps_failing_escalates_by_the_third_cycle``.

Two families of test here exist because of the store's shape rather than the
guard's logic, and they are the ones to break first if this file is ever
trimmed:

* ``test_*_keeps_*`` -- the latest event is a full-replacement snapshot, so a
  key a new row omits is a key that has been *deleted*. One test per field the
  payload used to drop.
* ``test_an_escalated_position_*`` -- spec 5.2's "stop attempting
  modifications" is a terminal state, not a per-cycle mood. Without it the
  first (true) escalation reason is buried under 86,400 later ones a day.

Every ``run()`` test drives the loop through ``_ScriptedVenue`` on an
already-set or immediately-set event with ``interval_seconds=0.0``: nothing in
this file may sleep or spin.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import pytest

from tests.unit.execution.conftest import (
    NOW,
    FakeProtectionVenue,
    RecordingEscalator,
    RecordingPositionStore,
)
from trading_house.core.clock import FixedClock
from trading_house.core.venue import PositionRecord
from trading_house.execution.guard import r_multiple
from trading_house.execution.loop import SYSTEM_TICKET, PositionGuard

OWNED_RANGES = ((110000, 110100),)

# A broker's own text, the kind `brokers/mt5/adapter.py` wraps into a
# `BrokerError` message. No payload may carry it; only the exception's type.
BROKER_TEXT = "login 81234567 on Broker-Live rejected: invalid password"


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


class _RaisingEscalator(RecordingEscalator):
    """N3: Task 6 wires ``escalate()`` to ``mark_stale()`` plus an audit-
    ledger append -- both database writes, either of which can fail
    independently of the cycle that triggered the report."""

    def escalate(self, reason: str, payload: Mapping[str, Any]) -> None:
        super().escalate(reason, payload)
        raise RuntimeError("escalator's own database write failed")


class _BrokerBlewUp(RuntimeError):
    """Stands in for ``brokers/mt5/adapter.py``'s ``BrokerError``, which the
    real port raises rather than returning None, and whose message can carry
    raw broker text."""


class _CountingStop(threading.Event):
    """A stop event that counts ``wait`` calls.

    Deleting the guard's ``stop.wait(interval_seconds)`` turns a once-a-second
    daemon into a 100%-CPU busy loop hammering the broker as fast as it can,
    and no assertion about cycle counts can see that.
    """

    def __init__(self) -> None:
        super().__init__()
        self.waits = 0

    def wait(self, timeout: float | None = None) -> bool:
        self.waits += 1
        return super().wait(timeout)


class _ScriptedVenue(FakeProtectionVenue):
    """A venue that scripts ``run()``: it counts reads, raises on the cycles
    named in ``raise_on``, and sets the stop event on read ``stop_after``. The
    event is always set from inside a read, so ``run()`` exits on its own
    without anything sleeping or spinning.
    """

    def __init__(
        self,
        stop: threading.Event,
        *,
        stop_after: int = 1,
        raise_on: frozenset[int] = frozenset(),
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._stop = stop
        self._stop_after = stop_after
        self._raise_on = raise_on
        self.reads = 0

    def positions_now(self) -> Sequence[PositionRecord] | None:
        self.reads += 1
        if self.reads >= self._stop_after:
            self._stop.set()
        if self.reads in self._raise_on:
            raise _BrokerBlewUp(BROKER_TEXT)
        return super().positions_now()


class _StoreLeakingTheDaemonRow(RecordingPositionStore):
    """A store whose ``open_positions()`` hands back the daemon's own reserved
    row. Task 2's query is expected to exclude it; the loop must not depend on
    that, because reading it back turns ticket 0 into a vanished position."""

    def open_positions(self) -> Sequence[Mapping[str, Any]]:
        return ({"position_ticket": SYSTEM_TICKET, "lifecycle": "GUARD_STOPPED"},)


def _guard(
    venue: FakeProtectionVenue,
    store: RecordingPositionStore,
    *,
    escalator: RecordingEscalator | None = None,
) -> PositionGuard:
    """``store`` is deliberately required: a shared default seed hid the very
    record-shape bugs this file exists to pin, so every test whose subject is
    the record seeds it in plain sight."""

    return PositionGuard(
        store=store,
        venue=venue,
        escalator=escalator if escalator is not None else RecordingEscalator(),
        clock=FixedClock(NOW),
        owned_magic_ranges=OWNED_RANGES,
        min_stop_distance=Decimal("0.00001"),
        default_stop_distance=Decimal("0.00300"),
    )


def _protected(store: RecordingPositionStore, **payload: str) -> RecordingPositionStore:
    """Seed ticket 7 as OPEN_PROTECTED. Every key is spelled out at the call
    site; this only saves the ticket and the lifecycle."""

    store.append(7, "OPEN_PROTECTED", NOW, payload)
    return store


def test_an_unreadable_position_list_escalates_and_acts_on_nothing() -> None:
    """None means the broker read FAILED. Treating it as "no positions" would
    conclude every position had closed and stop guarding all of them.

    ``report.escalated == 1`` alone is the literal 1 hardcoded in that same
    branch, so it is the escalator that has to be inspected.

    N5: this is a system-level escalation (no one position's business), and
    the module docstring promises this module owns the position-event write
    for every escalation directly -- not only the per-position ones."""

    venue = FakeProtectionVenue(positions=None)
    escalator = RecordingEscalator()
    store = RecordingPositionStore()

    report = _guard(venue, store, escalator=escalator).cycle()

    assert report.escalated == 1
    assert venue.amended == []
    assert [reason for reason, _ in escalator.calls] == [
        "could not read the broker's open positions"
    ]
    assert store.appended == [
        (SYSTEM_TICKET, "GUARD_ESCALATED", {"reason": "could not read the broker's open positions"})
    ]


def test_a_matching_stop_writes_nothing() -> None:
    store = _protected(RecordingPositionStore(), stop_loss="1.09700")

    _guard(FakeProtectionVenue(positions=[_observed(stop_loss=Decimal("1.09700"))]), store).cycle()

    assert len(store.appended) == 1  # only the seed


def test_a_matching_stop_with_a_risk_basis_writes_once_then_nothing() -> None:
    """D-2 at its real threat: a position with a risk basis computes an
    R-multiple every cycle. The first cycle moves an extremum off 0.0 and must
    write; a steady price after that must not, or this is 86,400 rows a day."""

    store = _protected(
        RecordingPositionStore(),
        stop_loss="1.09700",
        open_price="1.10000",
        initial_risk_distance="0.00300",
    )
    venue = FakeProtectionVenue(
        positions=[_observed(stop_loss=Decimal("1.09700"))],
        closing_price_result=Decimal("1.10150"),  # +0.5R
    )
    guard = _guard(venue, store)

    guard.cycle()
    after_first = len(store.appended)
    guard.cycle()
    guard.cycle()

    assert after_first == 2  # the seed plus one extrema row
    assert len(store.appended) == 2  # and nothing at a steady price after it
    assert store.appended[-1][2]["mfe_r"] == "0.5"


def test_a_missing_stop_is_restored_and_recorded() -> None:
    """The "and recorded" half needs the row counted and its stop read: the
    seed is already OPEN_PROTECTED, so asserting the lifecycle of
    ``appended[-1]`` is satisfied by the seed whether a row was written or
    not."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)])

    _guard(venue, store).cycle()

    assert venue.amended[0].stop_loss == Decimal("1.09700")
    assert len(store.appended) == 2
    assert store.appended[-1][1] == "OPEN_PROTECTED"
    assert store.appended[-1][2]["stop_loss"] == "1.09700"


def test_a_restore_that_keeps_failing_escalates_by_the_third_cycle() -> None:
    """I-21: restored or escalated within two cycles -- which is an upper AND
    a lower bound. Discarding the first two reports passes a guard that
    escalated on cycle two, and that is how D-7's "two failed attempts, not
    one" stops being tested at all."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], amend_fails=True)
    guard = _guard(venue, store)

    reports = [guard.cycle(), guard.cycle(), guard.cycle()]

    assert tuple(report.escalated for report in reports) == (0, 0, 1)


def test_a_failed_amend_still_counts_as_an_action() -> None:
    """``CycleReport.acted`` counts attempts, not successes -- pinned here
    because both readings are plausible and an unpinned one drifts."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], amend_fails=True)

    report = _guard(venue, store).cycle()

    assert venue.amended != []
    assert report.acted == 1


def test_shutdown_is_recorded() -> None:
    """A gap in the event stream must be explainable. An unrecorded stop looks
    identical to a guard that silently died."""

    store, stop = RecordingPositionStore(), threading.Event()
    stop.set()

    _guard(FakeProtectionVenue(), store).run(stop, interval_seconds=0.0)

    assert store.appended[-1][1] == "GUARD_STOPPED"


def test_run_runs_one_cycle_per_iteration_and_waits_between_them() -> None:
    """``run()``'s body is otherwise covered by nothing: a stop event that is
    already set when ``run()`` is called never executes it, so deleting both
    ``self.cycle()`` and ``stop.wait(interval_seconds)`` leaves the suite
    green -- the second one silently turning the daemon into a busy loop."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    stop = _CountingStop()
    venue = _ScriptedVenue(stop, positions=[_observed(stop_loss=Decimal("1.09700"))])

    _guard(venue, store).run(stop, interval_seconds=0.0)

    assert venue.reads == 1
    assert stop.waits == 1


def test_a_cycle_that_raises_escalates_and_the_daemon_keeps_running() -> None:
    """The real port RAISES -- ``adapter.py`` raises ``BrokerError`` whenever
    MT5's ``send_order`` returns None, which is any error. An uncaught one
    kills the daemon on the first flaky call, leaves every position unguarded,
    and produces exactly the unexplained gap in the event stream that
    ``test_shutdown_is_recorded`` exists to prevent.

    N5: the module docstring promises this module owns the position-event
    write for every escalation directly -- a raised cycle, filed under
    ``SYSTEM_TICKET``, included."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    stop = _CountingStop()
    escalator = RecordingEscalator()
    venue = _ScriptedVenue(
        stop,
        stop_after=3,
        raise_on=frozenset({2}),
        positions=[_observed(stop_loss=Decimal("1.09700"))],
    )

    _guard(venue, store, escalator=escalator).run(stop, interval_seconds=0.0)

    assert venue.reads == 3  # it survived cycle 2 and ran cycle 3
    assert [payload for _, payload in escalator.calls] == [{"error_type": "_BrokerBlewUp"}]
    assert BROKER_TEXT not in repr(escalator.calls)
    assert (
        SYSTEM_TICKET,
        "GUARD_ESCALATED",
        {"reason": "a cycle raised _BrokerBlewUp", "error_type": "_BrokerBlewUp"},
    ) in store.appended
    assert BROKER_TEXT not in repr(store.appended)
    assert store.appended[-1][1] == "GUARD_STOPPED"


def test_a_raising_escalator_does_not_kill_the_daemon() -> None:
    """N3: the escalate() call inside run()'s except handler must be
    contained on its own. Left unguarded, the escalator's own raise escapes
    the handler that was reporting a DIFFERENT failure, propagates out of the
    while loop, and kills the daemon thread -- I-6's promise (the guard
    outlives anything one cycle can do) held only for the venue, not for the
    escalator Task 6 wires to two more database writes."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    stop = _CountingStop()
    venue = _ScriptedVenue(
        stop,
        stop_after=2,
        raise_on=frozenset({1}),
        positions=[_observed(stop_loss=Decimal("1.09700"))],
    )
    escalator = _RaisingEscalator()

    _guard(venue, store, escalator=escalator).run(stop, interval_seconds=0.0)

    assert venue.reads == 2  # it survived both the cycle's raise and the escalator's
    assert escalator.calls != []  # the escalator was still tried
    assert store.appended[-1][1] == "GUARD_STOPPED"


def test_an_unavailable_price_still_protects_the_position() -> None:
    """R-multiples are analytics; the stop is the job. A guard that skipped
    restoring a missing stop because it could not read a quote would abandon a
    naked position over a number nobody trades on.

    The seed must carry the risk basis or the price is never read at all and
    ``closing_price_result=None`` is decoration -- hence ``price_reads``."""

    store = _protected(
        RecordingPositionStore(),
        stop_loss="1.09700",
        open_price="1.10000",
        initial_risk_distance="0.00300",
    )
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], closing_price_result=None)

    _guard(venue, store).cycle()

    assert venue.price_reads == [("EURUSD", True)]  # the branch was reached
    assert venue.amended[0].stop_loss == Decimal("1.09700")


def test_a_position_outside_our_magic_ranges_is_left_completely_alone() -> None:
    """A manually-opened trade, or another book's. Amending its stop would be
    this system reaching into a position it knows nothing about -- and MT5
    tags an untagged trade with magic 0, so this is the common case, not the
    exotic one."""

    store = RecordingPositionStore()
    venue = FakeProtectionVenue(positions=[_observed(magic=0, stop_loss=None)])

    report = _guard(venue, store).cycle()

    assert venue.amended == []
    assert store.appended == []
    assert report.checked == 0
    assert report.escalated == 0


def test_a_recorded_position_the_broker_disowns_is_not_declared_closed() -> None:
    """The ownership filter's failure mode is the dangerous direction. A
    ticket we hold a record of, reported by the broker under an out-of-range
    magic, is a LIVE position: filtering it only out of the observed side
    leaves it in the union as ``observed=None``, which reads as vanished and
    writes it CLOSED. One ``owned_magic_ranges`` edit away."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    venue = FakeProtectionVenue(positions=[_observed(magic=0, stop_loss=None)])

    report = _guard(venue, store).cycle()

    assert report.checked == 0
    assert venue.amended == []
    assert len(store.appended) == 1  # only the seed: nothing was recorded
    assert [lifecycle for _, lifecycle, _ in store.appended] == ["OPEN_PROTECTED"]


def test_adopting_an_orphan_executes_before_it_escalates() -> None:
    """Spec 5.2's escalation ends with "stop attempting modifications" --
    escalating before adopting would leave the orphan naked, the opposite of
    D-5's intent. This orphan has no broker-side stop at all, so decide()
    computes one at the default distance and a genuine write goes to the
    venue -- the scenario that actually exercises the ordering, unlike an
    orphan retaining its own stop, which (N1) is adopted without any amend at
    all (see ``test_an_orphan_retaining_its_own_stop_is_adopted_without_a_redundant_amend``)."""

    order: list[str] = []
    store = RecordingPositionStore()  # empty: ticket 7 is unrecorded, an orphan
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)])
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

    assert venue.amended[0].stop_loss == Decimal("1.09700")  # 1.10000 - 0.00300
    assert escalator.calls != []
    assert order == ["amend", "escalate"]
    assert report.escalated == 1


def test_an_orphan_retaining_its_own_stop_is_adopted_without_a_redundant_amend() -> None:
    """N1: this branch's target IS the broker's current stop already --
    decide() copies ``observed.stop_loss`` verbatim (D-5, guard.py:73-79).
    Sending that back to ``amend_protection`` asks the broker to change
    nothing, and adapter.py's ``_improves_on`` (I-8) rightly refuses a no-op
    -- reading that refusal as a failed write withheld the risk basis from a
    stop that was never wrong, silently losing r_multiple_open/mae_r/mfe_r
    for the position's entire life. The orphan is already protected at the
    target, so this is success, not a refusal to interpret."""

    store = RecordingPositionStore()  # empty: an orphan, but one with a stop
    venue = FakeProtectionVenue(
        positions=[_observed(stop_loss=Decimal("1.09700"))],
        closing_price_result=Decimal("1.10150"),  # +0.5R once the basis exists
    )
    escalator = RecordingEscalator()
    guard = _guard(venue, store, escalator=escalator)

    guard.cycle()  # adopts; establishes the risk basis; escalates per D-5
    guard.cycle()  # the extremum moves off 0.0 and forces a write

    assert venue.amended == []  # no amend was ever sent
    assert escalator.calls != []  # still escalated per D-5, just not via a write
    row = store.latest(7)
    assert row is not None
    assert row["open_price"] == "1.10000"
    assert row["initial_risk_distance"] == "0.00300"  # |1.10000 - 1.09700|
    assert row["mfe_r"] == "0.5"


def test_a_refused_adoption_does_not_fix_the_risk_basis_to_a_refused_stop() -> None:
    """Section 8.2 fixes ``initial_risk_distance`` at entry and never
    recomputes it. Deriving it from a stop the venue REJECTED would anchor
    every R-multiple this position ever reports, for its whole life, to
    protection that was never applied."""

    store = RecordingPositionStore()  # empty: an orphan with no broker stop
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], amend_fails=True)

    _guard(venue, store).cycle()

    row = store.appended[-1][2]
    assert venue.amended[0].stop_loss == Decimal("1.09700")  # it did try
    assert "initial_risk_distance" not in row
    assert "open_price" not in row


# --- The latest event is a full snapshot: one test per field it used to drop ---


def test_an_escalation_keeps_the_recorded_stop() -> None:
    """The D-7 path: two failed restores, then escalate. Escalation means
    "stop attempting modifications", so the latest event is the only place a
    human can read where the stop was supposed to be -- and it is written at
    exactly the moment the guard stops restoring it."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], amend_fails=True)
    guard = _guard(venue, store)

    guard.cycle()
    guard.cycle()
    guard.cycle()

    assert store.appended[-1][2]["stop_loss"] == "1.09700"


def test_a_refused_adoption_still_records_the_stop_it_tried() -> None:
    """The orphan's adopted stop went to the venue and was refused. If the row
    records no stop at all, the next cycle sees none on either side and
    escalates with "no stop anywhere to restore" -- having just computed one."""

    store = RecordingPositionStore()
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], amend_fails=True)

    _guard(venue, store).cycle()

    assert store.appended[-1][2]["stop_loss"] == "1.09700"  # 1.10000 - 0.00300


def test_a_material_write_keeps_the_take_profit() -> None:
    """Nothing in the loop ever emits ``take_profit``, so before this every
    material write dropped it -- and the loop's own ``amend_protection`` call
    reads it back from the record on the next cycle to send to the venue."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700", take_profit="1.10600")
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=Decimal("1.09900"))])

    _guard(venue, store).cycle()

    assert store.appended[-1][2] == {"stop_loss": "1.09900", "take_profit": "1.10600"}


def test_a_priceless_cycle_keeps_the_recorded_extrema() -> None:
    """A material change and an unreadable quote in the same cycle: the stop
    is recorded, and the trade-quality figures must survive a cycle that had
    nothing new to say about them."""

    store = _protected(
        RecordingPositionStore(),
        stop_loss="1.09700",
        open_price="1.10000",
        initial_risk_distance="0.00300",
        r_multiple_open="0.25",
        mae_r="-0.75",
        mfe_r="2.0",
    )
    venue = FakeProtectionVenue(
        positions=[_observed(stop_loss=Decimal("1.09900"))], closing_price_result=None
    )

    _guard(venue, store).cycle()

    row = store.appended[-1][2]
    assert row["stop_loss"] == "1.09900"  # the broker's value won, per spec 5
    assert (row["mae_r"], row["mfe_r"], row["r_multiple_open"]) == ("-0.75", "2.0", "0.25")


def test_the_closing_row_keeps_the_trade_quality_figures() -> None:
    """The CLOSED row is the one every downstream analysis reads. The broker no
    longer reports the position, so there is no price and no new R-multiple to
    compute -- which is precisely when the recorded ones matter."""

    store = _protected(
        RecordingPositionStore(),
        stop_loss="1.09700",
        open_price="1.10000",
        initial_risk_distance="0.00300",
        r_multiple_open="0.25",
        mae_r="-0.75",
        mfe_r="2.0",
    )
    venue = FakeProtectionVenue(positions=[])  # read fine; the position is gone

    _guard(venue, store).cycle()

    ticket, lifecycle, row = store.appended[-1]
    assert (ticket, lifecycle) == (7, "CLOSED")
    assert (row["mae_r"], row["mfe_r"]) == ("-0.75", "2.0")
    assert row["initial_risk_distance"] == "0.00300"


def test_a_record_holding_only_an_open_price_keeps_it() -> None:
    """``open_price`` and ``initial_risk_distance`` are separate fields and
    must carry forward separately. A half-populated record is what a position
    adopted mid-flight looks like, and losing the half it has makes the other
    half unrecoverable."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700", open_price="1.10000")
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=Decimal("1.09900"))])

    _guard(venue, store).cycle()

    assert store.appended[-1][2]["open_price"] == "1.10000"


# --- D-5's per-cycle escalate signal is not spec 5.2's terminal marker (N2) ---


def test_an_adopted_orphan_is_still_guarded_after_it_escalates() -> None:
    """N2: ``action.escalate`` (D-5's adopt-*then*-escalate signal, checked in
    ``_process``) and ``action.kind is ActionKind.ESCALATE`` (spec 5.2's
    terminal marker, checked separately in ``_maybe_record``) are two
    different conditions on purpose. Conflating them would mark every freshly
    adopted orphan terminal on its very first cycle -- abandoned rather than
    guarded, the opposite of D-5's intent. This orphan's stop vanishes again
    on the very next cycle; a still-guarded position restores it for real."""

    store = RecordingPositionStore()  # empty: ticket 7 is unrecorded, an orphan
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)])  # default-distance orphan
    escalator = RecordingEscalator()
    guard = _guard(venue, store, escalator=escalator)

    guard.cycle()  # adopts at the default distance; escalates per D-5
    assert escalator.calls != []
    assert len(venue.amended) == 1
    latest = store.latest(7)
    assert latest is not None
    assert latest.get("escalated") != "true"  # escalated per D-5, not terminal per 5.2

    venue._positions = [_observed(stop_loss=None)]  # the broker's stop vanishes again
    report = guard.cycle()

    assert len(venue.amended) == 2  # a second, genuine restore -- not abandoned
    assert report.acted == 1


# --- Escalation is terminal (spec 5.2's "stop attempting modifications") ---


def test_an_escalated_position_escalates_once_and_writes_one_row() -> None:
    """86,400 cycles a day. Without a terminal state an escalated position
    re-escalates -- ``mark_stale()`` plus an audit append -- and writes a row
    on every one of them, which is D-2's stated failure exactly: the event
    that matters buried under copies of itself."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], amend_fails=True)
    escalator = RecordingEscalator()
    guard = _guard(venue, store, escalator=escalator)

    for _ in range(5):
        guard.cycle()

    assert len(escalator.calls) == 1
    assert len(store.appended) == 2  # the seed plus one escalation row
    assert store.appended[-1][2]["escalated"] == "true"


def test_an_escalated_positions_reason_survives_later_cycles() -> None:
    """The first escalation carries the true diagnosis. Re-deciding an
    escalated position produces a different and wrong one every cycle after
    it, because by then the record no longer holds the stop that made the real
    reason possible."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], amend_fails=True)
    escalator = RecordingEscalator()
    guard = _guard(venue, store, escalator=escalator)

    for _ in range(5):
        guard.cycle()

    latest = store.latest(7)
    assert latest is not None
    assert latest["escalation_reason"] == "restore already failed twice"
    assert [reason for reason, _ in escalator.calls] == ["restore already failed twice"]


def test_an_escalated_position_is_still_escalated_after_a_restart() -> None:
    """A fresh guard over the same store must not start the whole sequence
    again: the flag lives in the event, not in the process."""

    store = _protected(RecordingPositionStore(), stop_loss="1.09700")
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=None)], amend_fails=True)
    first = _guard(venue, store)
    for _ in range(3):
        first.cycle()
    rows_at_escalation = len(store.appended)
    amends_at_escalation = len(venue.amended)

    escalator = RecordingEscalator()
    _guard(venue, store, escalator=escalator).cycle()

    assert len(store.appended) == rows_at_escalation
    assert len(venue.amended) == amends_at_escalation  # no further modification attempts
    assert escalator.calls == []


# --- The daemon's own reserved row is not a position (M3) ---


def test_a_restart_after_a_recorded_shutdown_writes_no_phantom_row() -> None:
    """``SYSTEM_TICKET`` files a system event -- an unreadable broker read, a
    shutdown -- under a ticket no real MT5 position can hold. Read back as an
    open position it is a ticket the broker does not report, which is the
    definition of vanished: the next run's first cycle would record ticket 0
    as a CLOSED position."""

    store = RecordingPositionStore()
    stop = threading.Event()
    stop.set()
    _guard(FakeProtectionVenue(), store).run(stop, interval_seconds=0.0)

    report = _guard(FakeProtectionVenue(), store).cycle()

    assert report.checked == 0
    assert [row for row in store.appended if row[0] == SYSTEM_TICKET] == [
        (SYSTEM_TICKET, "GUARD_STOPPED", {})
    ]


def test_the_daemon_ticket_is_skipped_even_if_the_store_hands_it_back() -> None:
    """Defence in depth for the above: Task 2's query is expected to exclude
    the reserved ticket, but the loop must not be the thing that breaks if it
    does not."""

    store = _StoreLeakingTheDaemonRow()

    report = _guard(FakeProtectionVenue(), store).cycle()

    assert report == type(report)(checked=0, acted=0, escalated=0)
    assert store.appended == []


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

    store = _protected(
        RecordingPositionStore(),
        stop_loss="1.09700",
        open_price="1.10000",
        initial_risk_distance="0.00300",
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

    store = _protected(
        RecordingPositionStore(),
        stop_loss="1.09700",
        open_price="1.10000",
        initial_risk_distance="0.00300",
    )
    venue = FakeProtectionVenue(positions=[_observed(stop_loss=Decimal("1.09900"))])

    _guard(venue, store).cycle()

    assert store.appended[-1][2]["initial_risk_distance"] == "0.00300"
