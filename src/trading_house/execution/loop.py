"""The daemon cycle: read the broker, read what we recorded, decide, act, record.

``PositionGuard.cycle()`` is I-21's engine. Once a cycle it (1) reads every
position the broker reports via ``ProtectionPort.positions_now()``, (2) reads
every position this system believes is still open via
``PositionStore.open_positions()``, (3) calls ``guard.decide()`` -- the pure
function -- on every ticket either side knows about, (4) executes the
verdict, and (5) appends a store row only when something material changed
(D-2): the stop moved, the position closed or was newly adopted, or a running
MAE/MFE extremum moved.

Ownership (Addition 5): a broker-reported position whose ``magic`` falls
outside every range in ``owned_magic_ranges`` is not the guard's business --
a manually-opened trade, or another book's -- and is filtered out before
anything else runs: not amended, not recorded, not counted in
``CycleReport.checked``, not escalated over. See
``brokers/mt5/adapter.py``'s ``reconcile()`` for the same reasoning against
MT5's untagged-magic-is-0 default (not imported from here -- see the module
boundary note below).

Escalation (Addition 6, spec 5.2): the decided action is always executed
FIRST -- an ``ADOPT_ORPHAN``'s stop write must land before anything else, or
the orphan sits naked in the gap, the opposite of D-5's intent -- and only
then does ``Escalator.escalate()`` run. Escalation is exactly three things:
``mark_stale()``, an audit-ledger append, and a position event. This module
owns the third directly (the same store append every material change makes);
the first two are bundled behind the single ``Escalator.escalate()`` call,
which the composition root (Task 6) wires to ``Gateway.mark_stale`` and the
audit ledger. There is no fourth step, no alerting subsystem and no safe-mode
state machine here.

MAE/MFE storage: there is no in-memory cache of the running extrema on
``PositionGuard``. Every cycle re-reads the last-written ``mae_r``/``mfe_r``
back from the store (0.0/0.0 for a position that has never recorded one) and
writes a fresh row the moment either one moves -- even when the stop itself
is unchanged and no other material change fired. The alternative (extrema
held only in this process's memory, flushed whenever some OTHER material
write happens) loses every extremum reached since the last such write on a
guard restart, and -- more immediately -- cannot pass a position that sits at
a matching stop for its entire observed life: nothing else ever forces a
flush, so the extrema would never reach the store at all. Re-reading costs
one extra row on the cycle an extremum happens to move; D-2's
86,400-writes-a-day concern is about the ordinary matching-stop cycle, which
this does not touch (see ``test_a_matching_stop_writes_nothing``).

Module boundary: ``execution/`` may import only ``core/`` and ``database/``
from this project (``tests/acceptance/test_architecture.py``). So
``ProtectionPort`` cannot use ``brokers/base.py``'s ``Quote`` for its price
read, and ``PositionGuard`` cannot take a ``constitution.VenueBinding`` for
its magic ranges -- both arrive here as plain ``Decimal``/``tuple[int, int]``
values instead, the same discipline the risk engine already follows for ATR.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from trading_house.core.clock import Clock
from trading_house.core.venue import (
    ExecutionOutcome,
    Mt5VenueRef,
    PositionRecord,
    Venue,
    VenueRef,
)
from trading_house.execution.guard import ActionKind, GuardAction, decide, r_multiple

# A system-level event (an unreadable broker read, guard shutdown) is not
# about any one position, so it is filed under a ticket no real MT5 position
# can hold -- position tickets are positive.
SYSTEM_TICKET = 0
DEFAULT_LIFECYCLE = "OPEN_PROTECTED"
# decide() only ever returns these two kinds with a venue-side write to make;
# TIGHTEN_STOP comes from decide_tighten(), which this cycle never calls.
_ACTING_KINDS = frozenset({ActionKind.RESTORE_STOP, ActionKind.ADOPT_ORPHAN})


class PositionStore(Protocol):
    """What the guard needs of the position event store. Structural, so the
    loop is testable without a database -- Task 2 supplies the concrete
    PostgreSQL implementation and must satisfy this shape.

    ``open_positions()`` returns the latest event per ticket whose lifecycle
    is not ``CLOSED`` -- what we *recorded*. Do not confuse it with
    ``ProtectionPort.positions_now()``, which is what the broker *has*; the
    guard's whole job is comparing the two.
    """

    def append(
        self,
        position_ticket: int,
        lifecycle: str,
        event_time: datetime,
        payload: Mapping[str, str],
    ) -> None: ...
    def latest(self, position_ticket: int) -> Mapping[str, Any] | None: ...
    def open_positions(self) -> Sequence[Mapping[str, Any]]: ...


class ProtectionPort(Protocol):
    """The one thing execution/ needs from a broker to run the guard."""

    def positions_now(self) -> Sequence[PositionRecord] | None:
        """Every open position the broker reports. ``None`` means the read
        FAILED -- never "no positions"; treating a failed read as an empty
        book would conclude every position had closed."""

    def amend_protection(
        self, ref: VenueRef, stop_loss: Decimal, take_profit: Decimal | None
    ) -> ExecutionOutcome: ...

    def closing_price(self, server_symbol: str, is_buy: bool) -> Decimal | None:
        """The price this position would close at right now: bid for a long,
        ask for a short. ``None`` means the read FAILED, never "no price" --
        the caller must skip this cycle's R update, not the stop check."""


class Escalator(Protocol):
    def escalate(self, reason: str, payload: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class CycleReport:
    checked: int
    acted: int
    escalated: int


def _owned(magic: int, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(low <= magic <= high for low, high in ranges)


def _decimal_or_none(value: object) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def _current_stop_str(
    action: GuardAction,
    observed: PositionRecord | None,
    recorded_stop: Decimal | None,
    amend_succeeded: bool,
) -> str | None:
    """What the guard now believes the broker-side stop to be. An acting kind
    reports its target only when the venue actually accepted it -- a failed
    attempt must report the broker's real, unimproved stop, or the record
    would claim a protection that was never applied."""

    if action.kind is ActionKind.RECORD_ONLY:
        return str(action.stop_loss)  # decide() already set this to observed.stop_loss
    if action.kind in _ACTING_KINDS and amend_succeeded:
        return str(action.stop_loss)
    if observed is not None:
        return str(observed.stop_loss) if observed.stop_loss is not None else None
    return str(recorded_stop) if recorded_stop is not None else None


class PositionGuard:
    def __init__(
        self,
        store: PositionStore,
        venue: ProtectionPort,
        escalator: Escalator,
        clock: Clock,
        *,
        owned_magic_ranges: Sequence[tuple[int, int]],
        min_stop_distance: Decimal,
        default_stop_distance: Decimal | None = None,
    ) -> None:
        self._store = store
        self._venue = venue
        self._escalator = escalator
        self._clock = clock
        self._owned_magic_ranges = tuple(owned_magic_ranges)
        self._min_stop_distance = min_stop_distance
        self._default_stop_distance = default_stop_distance
        # In-memory only, and deliberately not persisted: a restart forgets a
        # streak of failed restores and simply starts counting again, which
        # only delays an eventual escalation -- unlike the MAE/MFE extrema
        # above, losing this counter never understates anything on record.
        self._failed_restores: dict[int, int] = {}

    def cycle(self) -> CycleReport:
        observed_all = self._venue.positions_now()
        if observed_all is None:
            self._escalator.escalate("could not read the broker's open positions", {})
            return CycleReport(checked=0, acted=0, escalated=1)

        observed_by_ticket = {
            p.position_ticket: p for p in observed_all if _owned(p.magic, self._owned_magic_ranges)
        }
        recorded_by_ticket = {
            int(row["position_ticket"]): row for row in self._store.open_positions()
        }

        checked = acted = escalated = 0
        for ticket in sorted(set(observed_by_ticket) | set(recorded_by_ticket)):
            checked += 1
            did_act, did_escalate = self._process(
                ticket, observed_by_ticket.get(ticket), recorded_by_ticket.get(ticket)
            )
            acted += did_act
            escalated += did_escalate

        return CycleReport(checked=checked, acted=acted, escalated=escalated)

    def run(self, stop: threading.Event, interval_seconds: float = 1.0) -> None:
        while not stop.is_set():
            self.cycle()
            stop.wait(interval_seconds)
        self._store.append(SYSTEM_TICKET, "GUARD_STOPPED", self._clock.now(), {})

    def _process(
        self,
        ticket: int,
        observed: PositionRecord | None,
        record: Mapping[str, Any] | None,
    ) -> tuple[bool, bool]:
        is_recorded = record is not None
        recorded_stop = _decimal_or_none(record.get("stop_loss")) if record else None

        action = decide(
            observed=observed,
            is_recorded=is_recorded,
            is_buy=observed.is_buy if observed is not None else True,
            recorded_stop=recorded_stop,
            min_stop_distance=self._min_stop_distance,
            default_stop_distance=self._default_stop_distance,
            failed_restores=self._failed_restores.get(ticket, 0),
        )

        acted = False
        amend_succeeded = False
        # Every kind in _ACTING_KINDS is only ever returned by decide() once
        # its own `observed is None` branch has already returned, so observed
        # and action.stop_loss are always present together with an acting
        # kind -- the `and` guards below are for mypy, not a real fallback.
        if action.kind in _ACTING_KINDS and observed is not None and action.stop_loss is not None:
            acted = True
            ref = Mt5VenueRef(
                venue=Venue.MT5,
                magic=observed.magic,
                server_symbol=observed.server_symbol,
                position_ticket=ticket,
            )
            take_profit = _decimal_or_none(record.get("take_profit")) if record else None
            outcome = self._venue.amend_protection(ref, action.stop_loss, take_profit)
            amend_succeeded = outcome.accepted
            if action.kind is ActionKind.RESTORE_STOP:
                failed = self._failed_restores.get(ticket, 0)
                self._failed_restores[ticket] = 0 if amend_succeeded else failed + 1

        r_open, mae, mfe, extrema_changed = self._update_extrema(observed, record)

        self._maybe_record(
            ticket,
            action,
            observed,
            record,
            recorded_stop,
            amend_succeeded,
            r_open,
            mae,
            mfe,
            extrema_changed,
        )

        escalate_now = action.escalate or action.kind is ActionKind.ESCALATE
        if escalate_now:
            self._escalator.escalate(
                action.reason, {"position_ticket": ticket, "action": action.kind.value}
            )

        return acted, escalate_now

    def _update_extrema(
        self, observed: PositionRecord | None, record: Mapping[str, Any] | None
    ) -> tuple[str | None, str, str, bool]:
        """This cycle's R-multiple and the running MAE/MFE extrema. ``None``
        for the first element means no price and no risk basis were
        available this cycle -- the caller must not print an R figure it
        does not have (Addition 3: a missing price never blocks the stop
        check, it only skips this)."""

        if record is None or "open_price" not in record or "initial_risk_distance" not in record:
            return None, "0.0", "0.0", False
        prev_mae = float(record.get("mae_r", 0.0))
        prev_mfe = float(record.get("mfe_r", 0.0))
        if observed is None:
            return None, str(prev_mae), str(prev_mfe), False

        price = self._venue.closing_price(observed.server_symbol, observed.is_buy)
        if price is None:
            return None, str(prev_mae), str(prev_mfe), False

        current_r = r_multiple(
            is_buy=observed.is_buy,
            open_price=Decimal(str(record["open_price"])),
            current=price,
            initial_risk_distance=Decimal(str(record["initial_risk_distance"])),
        )
        mae = min(prev_mae, current_r)
        mfe = max(prev_mfe, current_r)
        return str(current_r), str(mae), str(mfe), (mae != prev_mae or mfe != prev_mfe)

    def _risk_basis(
        self, action: GuardAction, observed: PositionRecord | None, record: Mapping[str, Any] | None
    ) -> tuple[str | None, str | None]:
        """(open_price, initial_risk_distance) as the exact strings to
        persist -- never recomputed once set (Section 8.2). An already
        recorded distance is copied forward verbatim; only a freshly adopted
        orphan establishes one, from the stop it was just given."""

        if record is not None and "open_price" in record and "initial_risk_distance" in record:
            return str(record["open_price"]), str(record["initial_risk_distance"])
        if (
            action.kind is ActionKind.ADOPT_ORPHAN
            and observed is not None
            and action.stop_loss is not None
        ):
            distance = abs(observed.open_price - action.stop_loss)
            return str(observed.open_price), str(distance)
        return None, None

    def _maybe_record(
        self,
        ticket: int,
        action: GuardAction,
        observed: PositionRecord | None,
        record: Mapping[str, Any] | None,
        recorded_stop: Decimal | None,
        amend_succeeded: bool,
        r_open: str | None,
        mae: str,
        mfe: str,
        extrema_changed: bool,
    ) -> bool:
        """D-2: write a row only on material change -- a moved stop, a newly
        adopted or closed position, an escalation, or a moved MAE/MFE
        extremum. A matching stop with nothing else moving writes nothing."""

        materially_changed = (
            action.kind
            in (
                ActionKind.RECORD_ONLY,
                ActionKind.RECORD_CLOSED,
                ActionKind.ESCALATE,
                ActionKind.ADOPT_ORPHAN,
            )
            or (action.kind is ActionKind.RESTORE_STOP and amend_succeeded)
            or extrema_changed
        )
        if not materially_changed:
            return False

        payload: dict[str, str] = {}
        stop = _current_stop_str(action, observed, recorded_stop, amend_succeeded)
        if stop is not None:
            payload["stop_loss"] = stop

        open_price, distance = self._risk_basis(action, observed, record)
        if open_price is not None:
            payload["open_price"] = open_price
        if distance is not None:
            payload["initial_risk_distance"] = distance
        if r_open is not None:
            payload["r_multiple_open"] = r_open
            payload["mae_r"] = mae
            payload["mfe_r"] = mfe

        lifecycle = (
            "CLOSED"
            if action.kind is ActionKind.RECORD_CLOSED
            else str(record.get("lifecycle", DEFAULT_LIFECYCLE))
            if record
            else DEFAULT_LIFECYCLE
        )
        self._store.append(ticket, lifecycle, self._clock.now(), payload)
        return True
