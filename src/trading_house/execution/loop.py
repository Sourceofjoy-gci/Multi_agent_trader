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

Error containment (I-6), in three layers, because each one lets a different
promise through. The real ``ProtectionPort`` RAISES rather than returning
``None`` on a broken call -- ``brokers/mt5/adapter.py``'s
``amend_protection`` raises ``ConfigurationError``, ``BrokerUnavailableError``
and ``BrokerError`` (the last whenever MT5's ``send_order`` returns ``None``,
which it does on any error), and the gateway behind it raises
``BrokerUnavailableError`` and ``NonDemoAccountError``. All five are
``core.errors.TradingHouseError``.

1. **At the amend site**, that one base class is caught and the raise is
   treated as a failed write -- the same as a rejected ``ExecutionOutcome``,
   because a refusal that arrives as an exception is no less a refusal. If
   the raise did not advance ``_failed_restores``, D-7's "two failed restores
   escalate" would be unreachable through the path production actually takes:
   the guard would retry the same naked position every second forever. The
   exception's *type name* is remembered per ticket and travels with that
   ticket's eventual escalation payload, so the escalation says which kind of
   broker failure produced it. Anything not a ``TradingHouseError`` is a bug,
   not a broker refusal, and is deliberately left to fall through to (2).
2. **Around each ticket's ``_process``**, ``cycle()`` catches ``Exception``
   and moves on to the next ticket. I-21 is *every* open position, *every*
   cycle: without this, one ticket's failure costs every ticket sorted after
   it its check, for as long as the condition recurs.
3. **Around each ``cycle()``**, ``run()`` catches ``Exception`` -- no
   narrower, because the point is that the guard outlives anything one cycle
   can do.

(2) and (3) escalate with the exception's *type name* only. Never its
message: a broker's own text can be inside it, and no credential, DSN,
account number or raw broker message may reach any payload. Both go through
``_escalate_system``, which writes one row per unbroken run of the same
condition (D-2) rather than ~86,400 copies a day of an MT5 terminal that is
simply switched off, and writes again the moment the condition changes. The
shutdown row is written from a ``try/finally`` around the whole loop so it
lands even on an exit nothing here anticipated.

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
from trading_house.core.errors import TradingHouseError
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
# The kinds that are a material change on their own (D-2). RESTORE_STOP is
# deliberately absent: it is material only when the venue accepted the write,
# which is a runtime fact, not a property of the kind -- see _maybe_record.
_RECORDING_KINDS = frozenset(
    {
        ActionKind.RECORD_ONLY,
        ActionKind.RECORD_CLOSED,
        ActionKind.ESCALATE,
        ActionKind.ADOPT_ORPHAN,
    }
)
# Every kind this cycle knows what to do with. TIGHTEN_STOP is the one that is
# not here: decide() never returns it and this cycle never calls
# decide_tighten(), so a tighten arriving here means the next phase's trailing
# caller routed one through without teaching either set above about it. Raising
# is what stops that being dropped in silence (M2); containment turns it into
# an escalation rather than a dead daemon.
_HANDLED_KINDS = _ACTING_KINDS | _RECORDING_KINDS | frozenset({ActionKind.NOTHING})


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
        # The type name of the last amend that RAISED for this ticket, so the
        # escalation D-7 eventually fires can say which kind of broker failure
        # produced it. Type names only -- never a message.
        self._amend_errors: dict[int, str] = {}
        # D-2 for the daemon's own escalations: the reason last reported, so an
        # unchanged condition is reported once rather than once a second.
        self._last_system_reason: str | None = None
        # Per-ticket, unlike _last_system_reason: a raise inside one position's
        # check says nothing about the others (see cycle()).
        self._ticket_errors: dict[int, str] = {}

    def _escalate_system(
        self, reason: str, payload: Mapping[str, str], *, remember: bool = True
    ) -> bool:
        """Escalate a condition that is the daemon's, not one position's, and
        say whether it was actually reported.

        Escalation is exactly ``escalate()`` (Task 6 wires it to
        ``mark_stale()`` plus an audit append) and the position event this
        module writes itself, filed under ``SYSTEM_TICKET`` -- the same three
        things everywhere, no fourth step.

        D-2: an unreadable terminal is an ordinary overnight state, and
        reporting it every cycle is ~86,400 audit rows and ~86,400 position
        events a day, all identical, which is exactly how the one row that
        matters gets buried. So the reason is reported once per unbroken run
        of it and again the moment it changes -- including back to it after a
        clean cycle (unreadable -> readable -> unreadable). ``cycle()`` clears
        the memory on any cycle that had nothing system-level to say.

        Both calls are suppressed together (M3): Task 6 wires each to a
        database write, and a guard that cannot report a problem must still
        keep protecting positions (I-6).

        The memory is set only AFTER both land. Setting it first meant one
        failed report -- an audit-database blip -- turned a persistent outage
        into permanent silence, because the condition never changed and so was
        never re-reported. Retrying costs a duplicate audit row if escalate()
        succeeds and the append does not; that is an honest record of a
        repeated attempt, and the better failure of the two.

        ``remember=False`` is for callers holding their own per-ticket memory
        (see ``cycle()``), which must not collapse into the cycle-wide one.
        """

        if remember and reason == self._last_system_reason:
            return False
        recorded = False
        with contextlib.suppress(Exception):
            self._escalator.escalate(reason, payload)
            self._store.append(
                SYSTEM_TICKET,
                SYSTEM_ESCALATED_LIFECYCLE,
                self._clock.now(),
                {"reason": reason, **payload},
            )
            recorded = True
        if remember and recorded:
            self._last_system_reason = reason
        return True

    def cycle(self) -> CycleReport:
        observed_all = self._venue.positions_now()
        if observed_all is None:
            # N5: the module docstring promises this module writes the
            # position event directly for every escalation, system-level
            # ones included -- an unreadable read is exactly the kind most
            # worth finding later, filed under SYSTEM_TICKET (loop.py:104-107).
            reported = self._escalate_system("could not read the broker's open positions", {})
            return CycleReport(checked=0, acted=0, escalated=int(reported))

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
            try:
                did_act, did_escalate = self._process(
                    ticket, observed_by_ticket.get(ticket), recorded_by_ticket.get(ticket)
                )
            except Exception as exc:
                # I-21 is every open position, every cycle. Letting this out
                # would cost every ticket sorted after this one its check --
                # that cycle and, since the condition recurs, every cycle
                # after. Type name only: a broker's own text can be in the
                # message. Filed under SYSTEM_TICKET rather than the position's
                # own, because the store's latest event is a full-replacement
                # snapshot and a row this thin would DELETE the record that
                # says where the stop belongs.
                error_type = type(exc).__name__
                # Remembered per TICKET, not cycle-wide. Sharing
                # _last_system_reason would mean N tickets failing with the
                # same exception type write one row ever, naming the first --
                # so fixing ticket 7 would leave ticket 9 failing in permanent
                # silence. Keyed this way, each ticket reports once per
                # unbroken run of the same failure, which is still D-2.
                if self._ticket_errors.get(ticket) != error_type:
                    self._ticket_errors[ticket] = error_type
                    escalated += int(
                        self._escalate_system(
                            f"a position check raised {error_type}",
                            {"ticket": str(ticket), "error_type": error_type},
                            remember=False,
                        )
                    )
                continue
            acted += did_act
            escalated += did_escalate
            self._ticket_errors.pop(ticket, None)

        # Reaching here means positions_now() succeeded, so the one condition
        # _last_system_reason ever holds -- an unreadable broker -- is over,
        # and the next occurrence must be reported again (D-2: "and again the
        # moment it changes"). Deliberately NOT gated on whether some ticket
        # raised: per-ticket failures keep their own memory (_ticket_errors)
        # and say nothing about the broker read. Gating on them left this
        # stale, so an unreadable -> one-ticket-bug -> unreadable sequence
        # silently swallowed the second report.
        self._last_system_reason = None
        return CycleReport(checked=checked, acted=acted, escalated=escalated)

    def run(self, stop: threading.Event, interval_seconds: float = 1.0) -> None:
        try:
            while not stop.is_set():
                try:
                    self.cycle()
                except Exception as exc:
                    # The outermost of the three containment layers (see the
                    # module docstring): whatever the two inner ones did not
                    # hold must still not kill the daemon -- every position
                    # would go unguarded with no record of why. The
                    # exception's TYPE name is the diagnostic; its message
                    # never reaches a payload -- a broker's own text can be
                    # inside it. No retry, no backoff -- `finally` below still
                    # explains any eventual exit.
                    error_type = type(exc).__name__
                    self._escalate_system(
                        f"a cycle raised {error_type}", {"error_type": error_type}
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

        if observed is not None and observed.stop_loss is not None:
            # The stop is there NOW, so whatever happened on earlier cycles is
            # spent. Without this the counter is cleared only by an ACCEPTED
            # amend -- and BrokerError means "outcome unknown", not "rejected"
            # (core/errors.py), so an amend that raised but actually landed
            # left a permanent +1. Two of those on one ticket and the next
            # genuine stop loss escalates at D-7's threshold without ever
            # attempting the restore that would probably have worked, which
            # exempts a live position from I-21 permanently. Clearing here
            # keeps D-7 reachable genuinely while stopping it being reached
            # spuriously.
            self._failed_restores.pop(ticket, None)
            self._amend_errors.pop(ticket, None)

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
        if action.kind not in _HANDLED_KINDS:
            # M2: the one kind this is, TIGHTEN_STOP, is not silently dropped
            # by the two sets above disagreeing about it. cycle()'s per-ticket
            # containment turns this into an escalation, not a dead daemon.
            raise NotImplementedError(f"the guard cycle does not handle {action.kind.value}")

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
                try:
                    outcome = self._venue.amend_protection(ref, action.stop_loss, take_profit)
                except TradingHouseError as exc:
                    # C1: the production port RAISES where this fake-friendly
                    # signature returns -- adapter.py raises BrokerError
                    # whenever MT5's send_order returns None, which is any
                    # error, plus BrokerUnavailableError and
                    # ConfigurationError, and the gateway behind it adds
                    # BrokerUnavailableError and NonDemoAccountError. All are
                    # TradingHouseError. A raise is a failed write, exactly
                    # like a rejected outcome: counted, so D-7's two-strikes
                    # escalation is reachable through the path production
                    # actually takes. Type name only -- a broker's own text
                    # can be in the message, and no raw broker message may
                    # reach a payload. Anything NOT a TradingHouseError is a
                    # bug rather than a broker refusal and is left to
                    # cycle()'s containment.
                    amend_succeeded = False
                    self._amend_errors[ticket] = type(exc).__name__
                else:
                    amend_succeeded = outcome.accepted
                    if amend_succeeded:
                        self._amend_errors.pop(ticket, None)
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
            payload: dict[str, Any] = {"position_ticket": ticket, "action": action.kind.value}
            # C1: "restore already failed twice" says nothing about WHY. When
            # the failures arrived as exceptions, the last one's type name --
            # never its message -- names the kind of broker failure behind it,
            # which is the difference between a diagnosable escalation and a
            # shrug.
            amend_error = self._amend_errors.get(ticket)
            if amend_error is not None:
                payload["amend_error"] = amend_error
            self._escalator.escalate(action.reason, payload)

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
        ever reports to protection that was never applied.

        C2: an orphan whose broker stop sits AT its entry -- a stop moved to
        breakeven, an ordinary state -- yields a distance of zero. Zero is
        written once and then divided by on every later cycle
        (``r_multiple``), forever, since Section 8.2 never recomputes it: that
        ticket raises every second and, before ``cycle()``'s per-ticket
        containment, took every other position's check down with it. The guard
        belongs here rather than at the division because here is where the
        poison would be PERSISTED; guarding only the consumer would leave a
        meaningless basis on the record that nothing is allowed to correct.
        The position simply carries no R figures -- those are analytics, and
        the stop is the job."""

        if record is not None and "open_price" in record and "initial_risk_distance" in record:
            return str(record["open_price"]), str(record["initial_risk_distance"])
        if (
            action.kind is ActionKind.ADOPT_ORPHAN
            and amend_succeeded
            and observed is not None
            and action.stop_loss is not None
        ):
            distance = abs(observed.open_price - action.stop_loss)
            if distance != 0:
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
            action.kind in _RECORDING_KINDS
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
