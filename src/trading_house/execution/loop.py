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
``CycleReport.checked``, not escalated over. That holds however we came to
learn about the ticket -- a disowned ticket we also hold a RECORD of must be
excluded from the union of tickets to process too, or it reads as vanished
(the recorded side still has it, the observed side no longer does) and gets
declared CLOSED, the dangerous direction: a live position the guard stops
guarding. See ``brokers/mt5/adapter.py``'s ``reconcile()`` for the same
reasoning against MT5's untagged-magic-is-0 default (not imported from here
-- see the module boundary note below).

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

When ``decide()`` itself returns ``ActionKind.ESCALATE`` (D-7: two failed
restores, or an orphan with no stop and no distance), that position event
also carries ``escalated="true"`` and ``escalation_reason``. Spec 5.2's "then
stop attempting modifications" is a TERMINAL state, not a per-cycle mood: any
later cycle whose latest record carries ``escalated`` returns immediately
from ``_process`` -- no ``decide()``, no amend, no row, no escalate call --
so the true diagnosis is not buried under thousands of repetitions of itself,
each one wrong because the record no longer holds what made the first one
possible. There is deliberately no sixth ``PositionState.lifecycle`` value
for this (``core/schemas.py:238`` closes that ``Literal`` at five); it lives
in the payload instead.

**Nothing clears it -- not this module and not anything else in this system.**
The flag is carried forward by every later write (see ``_maybe_record``) and
the store is append-only, so it is terminal for the life of that position's
record, and an escalated position is therefore exempt from I-21 until a human
intervenes. ``mark_stale()``/``mark_reconciled()`` govern whether *gateway*
state is trusted and never touch a position record, so a successful reconcile
does not clear this despite looking like it should. Whether escalation ought to
be clearable is an open design question, deliberately not answered here.

Error containment (I-6): the real ``ProtectionPort`` RAISES rather than
returning ``None`` on a broken call -- ``brokers/mt5/adapter.py``'s
``amend_protection`` raises ``ConfigurationError``, ``BrokerUnavailableError``
and ``BrokerError`` (the last whenever MT5's ``send_order`` returns ``None``,
which it does on any error). ``run()`` therefore wraps each ``cycle()`` in
``try/except Exception`` -- no narrower, because the point is that the guard
outlives anything one cycle can do -- and escalates with the exception's
*type name* only. Never its message: a broker's own text can be inside it,
and no credential, DSN, account number or raw broker message may reach any
payload. The shutdown row is written from a ``try/finally`` around the whole
loop so it lands even on an exit nothing here anticipated.

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

import contextlib
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
# The lifecycle a system-level escalation (no one position's business) is
# filed under, alongside GUARD_STOPPED -- both daemon-only, neither one of
# core/schemas.py:238's five PositionState values (see PositionStore's
# docstring: position_events.lifecycle is deliberately a superset).
SYSTEM_ESCALATED_LIFECYCLE = "GUARD_ESCALATED"
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

    ``position_events.lifecycle`` (the column this store persists) is
    deliberately a SUPERSET of ``PositionState.lifecycle``
    (``core/schemas.py:238``, a closed five-value ``Literal``): this store
    also carries daemon-only rows this system writes under ``SYSTEM_TICKET``
    (``"GUARD_STOPPED"``) that no position state machine ever needs. Task 2's
    migration must not constrain this column to the five.

    Row shape: what ``latest()`` and each element of ``open_positions()``
    return is exactly the last-appended payload's own fields plus
    ``position_ticket`` and ``lifecycle`` -- nothing structural. This loop
    carries a fetched row forward verbatim into its next write (see
    ``_maybe_record``), so any extra structural column (an event id,
    ``event_time``, ``created_at``) would get copied into every later
    payload, stringified with ``str()`` -- a Python repr for anything
    nested, landing in a durable, operator-facing record.
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
    acted: int  # amend ATTEMPTS, not successes -- see test_a_failed_amend_still_counts_as_an_action
    escalated: int


def _owned(magic: int, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(low <= magic <= high for low, high in ranges)


def _decimal_or_none(value: object) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def _current_stop_str(
    action: GuardAction,
    observed: PositionRecord | None,
    recorded_stop: Decimal | None,
) -> str | None:
    """What the guard now believes the broker-side stop to be, to overlay
    onto the carried-forward record (see ``_maybe_record``).

    ``RECORD_ONLY`` and an acting kind (``RESTORE_STOP``, ``ADOPT_ORPHAN``)
    both report their target verbatim -- whether or not the venue accepted
    it. For ``RESTORE_STOP`` the target IS the already-recorded stop, so a
    refusal changes nothing either way. For ``ADOPT_ORPHAN`` the target is
    brand new: a fresh orphan has no prior record to fall back on, so
    withholding it on a refusal would lose the computed value entirely and
    leave the next cycle believing there is no stop anywhere, escalating with
    the wrong diagnosis having just computed the right one (see
    ``test_a_refused_adoption_still_records_the_stop_it_tried``).

    Whether the target actually landed at the broker is a separate question,
    answered by whether the *risk basis* gets established -- ``_risk_basis``
    is the one that stays gated on ``amend_succeeded``, because it is never
    recomputed once set (Section 8.2), so anchoring it to a refused stop
    would be permanent.
    """

    if action.kind is ActionKind.RECORD_ONLY or action.kind in _ACTING_KINDS:
        return str(action.stop_loss) if action.stop_loss is not None else None
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
        min_stop_distances: Mapping[str, Decimal],
        default_stop_distances: Mapping[str, Decimal | None],
    ) -> None:
        self._store = store
        self._venue = venue
        self._escalator = escalator
        self._clock = clock
        self._owned_magic_ranges = tuple(owned_magic_ranges)
        # Both distances are per-INSTRUMENT quantities, keyed by server
        # symbol: the minimum is that contract's stops level, and the default
        # is the book's signed k_sigma against that instrument's own ATR. One
        # scalar for the whole book would adopt a live position at a distance
        # derived from a different instrument's volatility -- exactly the
        # fabricated number D-5 forbids, arriving through the back door.
        # Scoping the daemon to one instrument instead would leave every
        # position outside that scope silently unguarded, which I-21 forbids.
        self._min_stop_distances = dict(min_stop_distances)
        self._default_stop_distances = dict(default_stop_distances)
        # In-memory only, and deliberately not persisted: a restart forgets a
        # streak of failed restores and simply starts counting again, which
        # only delays an eventual escalation -- unlike the MAE/MFE extrema
        # above, losing this counter never understates anything on record.
        self._failed_restores: dict[int, int] = {}

    def cycle(self) -> CycleReport:
        observed_all = self._venue.positions_now()
        if observed_all is None:
            reason = "could not read the broker's open positions"
            self._escalator.escalate(reason, {})
            # N5: the module docstring promises this module writes the
            # position event directly for every escalation, system-level
            # ones included -- an unreadable read is exactly the kind most
            # worth finding later, filed under SYSTEM_TICKET (loop.py:104-107).
            self._store.append(
                SYSTEM_TICKET, SYSTEM_ESCALATED_LIFECYCLE, self._clock.now(), {"reason": reason}
            )
            return CycleReport(checked=0, acted=0, escalated=1)

        observed_all_by_ticket = {p.position_ticket: p for p in observed_all}
        owned_tickets = {
            ticket
            for ticket, p in observed_all_by_ticket.items()
            if _owned(p.magic, self._owned_magic_ranges)
        }
        # A ticket the broker reports under an out-of-range magic is not
        # ours (Addition 5), however we came to also hold a record of it --
        # excluded from the union entirely, not only from the observed side,
        # or it reads as vanished and gets declared CLOSED.
        disowned_tickets = set(observed_all_by_ticket) - owned_tickets
        observed_by_ticket = {t: observed_all_by_ticket[t] for t in owned_tickets}
        recorded_by_ticket = {
            int(row["position_ticket"]): row for row in self._store.open_positions()
        }

        checked = acted = escalated = 0
        tickets = (set(observed_by_ticket) | set(recorded_by_ticket)) - disowned_tickets
        # SYSTEM_TICKET is the daemon's own reserved row, never a real
        # position -- skipped here defensively even though the store's own
        # open_positions() is expected to exclude it already (M3): this loop
        # must not depend on that.
        for ticket in sorted(tickets - {SYSTEM_TICKET}):
            checked += 1
            did_act, did_escalate = self._process(
                ticket, observed_by_ticket.get(ticket), recorded_by_ticket.get(ticket)
            )
            acted += did_act
            escalated += did_escalate

        return CycleReport(checked=checked, acted=acted, escalated=escalated)

    def run(self, stop: threading.Event, interval_seconds: float = 1.0) -> None:
        try:
            while not stop.is_set():
                try:
                    self.cycle()
                except Exception as exc:
                    # The real port RAISES (adapter.py's BrokerError etc.)
                    # rather than returning None, so one flaky broker call
                    # must not kill the daemon -- every position would go
                    # unguarded with no record of why. The exception's TYPE
                    # name is the diagnostic; its message never reaches a
                    # payload -- a broker's own text can be inside it.
                    with contextlib.suppress(Exception):
                        # N3: Task 6 wires escalate() to mark_stale() plus an
                        # audit-ledger append -- both database writes. A
                        # raising escalator must not do what the raising
                        # cycle itself could not (I-6): a guard that cannot
                        # report a problem must still keep protecting
                        # positions. No retry, no backoff -- `finally` below
                        # still explains any eventual exit.
                        error_type = type(exc).__name__
                        reason = f"a cycle raised {error_type}"
                        self._escalator.escalate(reason, {"error_type": error_type})
                        # N5: this module owns the position-event write for
                        # every escalation directly, system-level ones
                        # included -- a raised cycle is exactly the kind most
                        # worth finding later, filed under SYSTEM_TICKET. In
                        # the same suppressed block as escalate() above: this
                        # write must not do what the raising cycle could not
                        # either.
                        self._store.append(
                            SYSTEM_TICKET,
                            SYSTEM_ESCALATED_LIFECYCLE,
                            self._clock.now(),
                            {"reason": reason, "error_type": error_type},
                        )
                stop.wait(interval_seconds)
        finally:
            self._store.append(SYSTEM_TICKET, "GUARD_STOPPED", self._clock.now(), {})

    def _process(
        self,
        ticket: int,
        observed: PositionRecord | None,
        record: Mapping[str, Any] | None,
    ) -> tuple[bool, bool]:
        if record is not None and record.get("escalated") == "true":
            # Terminal (spec 5.2's "stop attempting modifications"): forms no
            # new opinion, attempts no amend, writes no row, escalates again
            # never. Nothing clears this -- not here and not elsewhere; the
            # flag is carried forward by every later write into an append-only
            # store. mark_reconciled() clears GATEWAY staleness, not a position
            # record, so a reconcile does not un-escalate this despite reading
            # as though it would.
            return False, False

        is_recorded = record is not None
        recorded_stop = _decimal_or_none(record.get("stop_loss")) if record else None

        # The distances belong to the instrument, so they are resolved from
        # the broker's own symbol for THIS position. A symbol with no
        # configured default yields None, which decide() escalates on ("orphan
        # has no stop and no distance") -- refusing to invent is the point:
        # the alternative is adopting at another instrument's volatility.
        # A symbol with no configured minimum is treated as a floor of zero,
        # which simply does not bind; that is a decision, and it is safe only
        # because the number that must never be borrowed is the default above,
        # which escalates instead of defaulting. `observed is None` means the
        # position is gone -- decide()'s vanished branch returns before it
        # reads either distance, so the empty key never reaches a decision.
        server_symbol = observed.server_symbol if observed is not None else ""
        action = decide(
            observed=observed,
            is_recorded=is_recorded,
            recorded_stop=recorded_stop,
            min_stop_distance=self._min_stop_distances.get(server_symbol, Decimal(0)),
            default_stop_distance=self._default_stop_distances.get(server_symbol),
            failed_restores=self._failed_restores.get(ticket, 0),
        )

        acted = False
        amend_succeeded = False
        # Every kind in _ACTING_KINDS is only ever returned by decide() once
        # its own `observed is None` branch has already returned, so observed
        # and action.stop_loss are always present together with an acting
        # kind -- the `and` guards below are for mypy, not a real fallback.
        if action.kind in _ACTING_KINDS and observed is not None and action.stop_loss is not None:
            if action.stop_loss == observed.stop_loss:
                # The target already IS the broker's current stop -- only
                # reachable via ADOPT_ORPHAN's retains-its-own-stop branch
                # (decide() sets stop_loss=observed.stop_loss there verbatim);
                # RESTORE_STOP's target is only ever chosen when
                # observed.stop_loss is None, so it can never match here.
                # Sending this anyway would ask amend_protection to modify
                # nothing, and adapter.py's _improves_on (I-8) rightly
                # refuses a no-op amend -- reading that refusal as a failed
                # write would withhold the risk basis from a stop that is
                # genuinely already correct. Skipping is not failing: the
                # position IS protected at the target, so there is no broker
                # call to make and no attempt to count as `acted`.
                amend_succeeded = True
            else:
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
        self,
        action: GuardAction,
        observed: PositionRecord | None,
        record: Mapping[str, Any] | None,
        amend_succeeded: bool,
    ) -> tuple[str | None, str | None]:
        """(open_price, initial_risk_distance) as the exact strings to
        persist -- never recomputed once set (Section 8.2). An already
        recorded distance is copied forward verbatim; only a freshly adopted
        orphan establishes one, from the stop it was just given -- and only
        when the venue actually accepted that stop. An ``ADOPT_ORPHAN`` the
        venue refused must not anchor every future R-multiple this position
        ever reports to protection that was never applied."""

        if record is not None and "open_price" in record and "initial_risk_distance" in record:
            return str(record["open_price"]), str(record["initial_risk_distance"])
        if (
            action.kind is ActionKind.ADOPT_ORPHAN
            and amend_succeeded
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
        extremum. A matching stop with nothing else moving writes nothing.

        The store's latest event is a full-replacement snapshot, not a merge
        (Task 2) -- so every row this writes must be a COMPLETE picture of
        the position, not just what this cycle changed, or every key this
        cycle does not independently re-emit is a key that gets DELETED. The
        payload starts as the prior record's own fields and only overlays
        what actually moved this cycle; six field-by-field patches would
        still miss every key a later schema adds (Task 2's server_symbol,
        magic, volume, intent_id, opened_at) -- carrying the whole snapshot
        forward does not.
        """

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

        payload: dict[str, str] = {
            key: str(value)
            for key, value in (record or {}).items()
            if key not in ("position_ticket", "lifecycle") and value is not None
        }

        stop = _current_stop_str(action, observed, recorded_stop)
        if stop is not None:
            payload["stop_loss"] = stop

        open_price, distance = self._risk_basis(action, observed, record, amend_succeeded)
        if open_price is not None:
            payload["open_price"] = open_price
        if distance is not None:
            payload["initial_risk_distance"] = distance
        if r_open is not None:
            payload["r_multiple_open"] = r_open
            payload["mae_r"] = mae
            payload["mfe_r"] = mfe

        if action.kind is ActionKind.ESCALATE:
            # Spec 5.2's terminal marker (C2): _process short-circuits for
            # any ticket whose latest record carries this. Deliberately a
            # payload field, not a sixth PositionState.lifecycle value --
            # core/schemas.py:238 closes that Literal at five.
            payload["escalated"] = "true"
            payload["escalation_reason"] = action.reason

        if action.kind is ActionKind.RECORD_CLOSED:
            lifecycle = "CLOSED"
        elif record is not None:
            lifecycle = str(record.get("lifecycle", DEFAULT_LIFECYCLE))
        else:
            lifecycle = DEFAULT_LIFECYCLE

        self._store.append(ticket, lifecycle, self._clock.now(), payload)
        return True
