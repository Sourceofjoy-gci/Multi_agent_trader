"""The position guard's two composed dependencies.

``execution/loop.py`` declares a ``ProtectionPort`` and an ``Escalator`` and
deliberately cannot build either: that package may import ``core/`` and
``database/`` and nothing else this project owns
(``tests/acceptance/test_architecture.py``), while the port needs
``brokers/`` and ``constitution/`` and the escalator needs ``audit/``. So
they live here, beside ``health.py``, which already joins the same four
packages for the readiness gate.

``cli.py`` is the composition root for everything else and would have been
the obvious home, but it is 869 lines against a 558-line next-largest module;
these two classes are the wiring, not the commands.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter
from trading_house.constitution.binding import VenueBinding
from trading_house.core.clock import Clock
from trading_house.core.errors import TradingHouseError
from trading_house.core.venue import ExecutionOutcome, PositionRecord, VenueRef
from trading_house.ops.health import AuditLedger, build_audit_event

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from trading_house.execution.loop import (
        Escalator as _GuardEscalator,
    )
    from trading_house.execution.loop import (
        ProtectionPort as _GuardProtectionPort,
    )

ESCALATION_EVENT = "guard.escalated"
_SOURCE_COMPONENT = "ops.guard"


class Mt5ProtectionPort:
    """``Mt5BrokerAdapter`` as the guard's ``ProtectionPort``.

    ``positions_now`` and ``amend_protection`` already match, shape for
    shape, and pass straight through -- ownership filtering and the
    at-most-one-attempt discipline are the guard's decisions, not this
    shim's. Only the price read needs adapting: the adapter offers
    ``snapshot(instrument_ids)``, keyed by our own neutral instrument ids,
    while the guard holds nothing but the broker's server symbol for a live
    position.
    """

    def __init__(self, adapter: Mt5BrokerAdapter, binding: VenueBinding) -> None:
        self._adapter = adapter
        # The signed binding is authoritative for symbol identity, here as
        # everywhere else. A symbol it does not name has no instrument and
        # therefore no price -- never some other instrument's.
        self._instrument_ids = {
            bound.server_symbol: instrument_id
            for instrument_id, bound in binding.instruments.items()
        }

    def positions_now(self) -> Sequence[PositionRecord] | None:
        return self._adapter.positions_now()

    def amend_protection(
        self, ref: VenueRef, stop_loss: Decimal, take_profit: Decimal | None
    ) -> ExecutionOutcome:
        return self._adapter.amend_protection(ref, stop_loss, take_profit)

    def closing_price(self, server_symbol: str, is_buy: bool) -> Decimal | None:
        """The price this position would close at right now: the BID for a
        long, the ASK for a short -- the side it would actually be filled on,
        which is the honest input to an R-multiple. The mid would flatter
        every open position by half a spread, forever.

        ``None`` means the read failed, which is the Protocol's own
        distinction and the reason a typed broker failure is caught here
        rather than raised: this number feeds MAE/MFE and nothing else, and
        letting it abort the cycle would take every other position's stop
        check down with it (I-21).
        """

        instrument_id = self._instrument_ids.get(server_symbol)
        if instrument_id is None:
            return None
        try:
            snapshot = self._adapter.snapshot([instrument_id])
        except TradingHouseError:
            return None
        quote = next((q for q in snapshot.quotes if q.instrument_id == instrument_id), None)
        if quote is None:
            # snapshot() skips a symbol with no current tick rather than
            # inventing a quote for it.
            return None
        return quote.bid if is_buy else quote.ask


class LedgerEscalator:
    """Spec 5.2's escalation, minus the position event the loop writes itself.

    Exactly two things happen here: the gateway is marked stale, and one row
    goes into the hash-chained audit ledger. There is no alerting subsystem
    and no safe-mode state machine in this codebase, and this class is not a
    stand-in for one -- an operator learns of an escalation by reading the
    audit ledger or running ``guard status``.

    ``payload`` is carried verbatim. The loop guarantees it contains only an
    exception's TYPE name, never its message, because a broker's own text --
    a login, a server name -- can be inside one; enriching it here with a
    connection detail or a broker read would undo that at the last step.
    """

    def __init__(self, ledger: AuditLedger, mark_stale: Callable[[], None], clock: Clock) -> None:
        self._ledger = ledger
        self._mark_stale = mark_stale
        self._clock = clock

    def escalate(self, reason: str, payload: Mapping[str, Any]) -> None:
        self._mark_stale()
        self._ledger.append(
            build_audit_event(
                ESCALATION_EVENT,
                self._clock.now(),
                {"reason": reason, **payload},
                source_component=_SOURCE_COMPONENT,
            )
        )


if TYPE_CHECKING:  # pragma: no cover

    def _conforms_to_the_guards_ports(
        port: Mt5ProtectionPort, escalator: LedgerEscalator
    ) -> tuple[_GuardProtectionPort, _GuardEscalator]:
        """Structural conformance, checked by mypy and erased at runtime --
        the same pin ``execution/positions.py`` uses for the store, and for
        the same reason: both are Protocols, so nothing else would catch a
        signature drifting out of shape until the daemon ran."""

        return port, escalator
