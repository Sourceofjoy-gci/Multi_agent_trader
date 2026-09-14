"""The guard's composition root: the two dependencies nothing else supplies.

``execution/`` may import ``core/`` and ``database/`` and nothing else this
project owns, so the adapter cannot satisfy ``ProtectionPort`` from inside it
and the escalator cannot reach the audit ledger from there either. Both live
outside that boundary, and these tests are the only place their two real
decisions are pinned: which side of the spread a position closes at, and what
an escalation is allowed to write.

``FakeTerminal`` is imported from the MT5 conftest by absolute path, which
that file's own docstring calls out as the supported way to reach it from
outside its directory.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

from tests.unit.brokers.mt5.conftest import FakeTerminal
from trading_house.audit.models import AuditEvent, AuditRecord, IntegrityReport
from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter
from trading_house.brokers.mt5.boundary import Mt5Position, Mt5Tick
from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.constitution.binding import parse_venue_binding
from trading_house.core.clock import FixedClock
from trading_house.core.venue import Mt5VenueRef, Venue
from trading_house.ops.guard import LedgerEscalator, Mt5ProtectionPort

NOW = datetime(2026, 9, 9, tzinfo=UTC)

BINDING = parse_venue_binding(
    b"""
venue: mt5
books:
  fx_scalp: {magic_range: [110000, 119999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD"}
"""
)

# One position inside fx_scalp's range, one in no declared range: the shim
# filters neither, because ownership is the guard's decision, not its.
POSITIONS = (
    Mt5Position(
        ticket=1001,
        magic=110042,
        server_symbol="EURUSD",
        volume=0.1,
        price_open=1.1000,
        sl=1.0950,
        tp=1.1100,
        is_buy=True,
        opened_at=NOW,
    ),
    Mt5Position(
        ticket=1002,
        magic=999999,
        server_symbol="EURUSD",
        volume=0.2,
        price_open=1.2000,
        sl=1.2050,
        tp=None,
        is_buy=False,
        opened_at=NOW,
    ),
)


class _TicklessTerminal(FakeTerminal):
    def symbol_tick(self, server_symbol: str) -> Mt5Tick | None:
        return None


class _RecordingLedger:
    """The append half of ``ops.health.AuditLedger``. ``verify`` belongs to
    that Protocol and no escalation ever calls it; the ``AuditRecord`` append
    returns is never read either."""

    def __init__(self) -> None:
        self.appended: list[AuditEvent] = []

    def verify(self) -> IntegrityReport:
        raise NotImplementedError

    def append(self, event: AuditEvent) -> AuditRecord:
        self.appended.append(event)
        return None  # type: ignore[return-value]


@contextmanager
def _port(terminal: FakeTerminal) -> Iterator[tuple[Mt5ProtectionPort, Mt5Gateway]]:
    gateway = Mt5Gateway(terminal, clock=FixedClock(NOW), request_timeout_seconds=5.0)  # type: ignore[arg-type]
    gateway.start()
    try:
        adapter = Mt5BrokerAdapter(gateway, BINDING, clock=FixedClock(NOW))
        yield Mt5ProtectionPort(adapter, BINDING), gateway
    finally:
        gateway.stop()


def test_a_long_closes_at_the_bid_and_a_short_at_the_ask() -> None:
    """The honest input to an R-multiple is the side the position would
    actually close at -- not the mid, and not the side it opened on. The fake
    terminal quotes bid 1.1 / ask 1.2, far enough apart that a mid or a
    swapped side shows up as a wrong number rather than a rounding."""

    with _port(FakeTerminal()) as (port, _gateway):
        assert port.closing_price("EURUSD", is_buy=True) == Decimal("1.1")
        assert port.closing_price("EURUSD", is_buy=False) == Decimal("1.2")


def test_a_symbol_outside_the_signed_binding_has_no_closing_price() -> None:
    """The binding is authoritative for symbol identity. An unmapped symbol
    has no price here -- never another instrument's, which would put an
    invented R-multiple on a real position."""

    with _port(FakeTerminal()) as (port, _gateway):
        assert port.closing_price("GBPUSD", is_buy=True) is None


def test_a_symbol_with_no_current_tick_has_no_closing_price() -> None:
    with _port(_TicklessTerminal()) as (port, _gateway):
        assert port.closing_price("EURUSD", is_buy=True) is None


def test_an_unreachable_terminal_is_a_missing_price_not_a_raised_cycle() -> None:
    """``None`` means the read FAILED -- the Protocol's own words. Letting a
    typed broker failure out of here instead would abort the whole cycle, and
    every OTHER position's stop check with it, over a number used only for
    analytics (I-21: every open position, every cycle)."""

    with _port(FakeTerminal()) as (port, gateway):
        gateway.stop()  # every later call raises BrokerUnavailableError

        assert port.closing_price("EURUSD", is_buy=True) is None


def test_positions_pass_straight_through() -> None:
    """Ownership filtering is the guard's job, not this shim's: both the
    owned and the unowned position arrive, exactly as the adapter reports
    them."""

    with _port(FakeTerminal(positions=POSITIONS)) as (port, _gateway):
        positions = port.positions_now()

    assert positions is not None
    assert sorted(position.position_ticket for position in positions) == [1001, 1002]


def test_an_amendment_reaches_the_adapter() -> None:
    """The adapter refuses to widen (I-8's outer layer), so a refused outcome
    is what proves the call arrived there rather than being answered here."""

    ref = Mt5VenueRef(venue=Venue.MT5, magic=110042, server_symbol="EURUSD", position_ticket=1001)

    with _port(FakeTerminal(positions=POSITIONS)) as (port, _gateway):
        outcome = port.amend_protection(ref, Decimal("1.0900"), None)

    assert outcome.accepted is False  # 1.0900 is WIDER than the position's 1.0950


def test_escalation_marks_the_gateway_stale_and_writes_one_audit_row() -> None:
    """Spec 5.2's escalation, minus the position event the loop writes itself:
    ``mark_stale()`` plus one hash-chained audit row, and nothing else. There
    is no alerting subsystem and no safe-mode state machine in this codebase
    -- an operator learns of this by reading the ledger or running
    ``guard status``."""

    stale: list[bool] = []
    ledger = _RecordingLedger()

    LedgerEscalator(ledger, lambda: stale.append(True), FixedClock(NOW)).escalate(
        "restore already failed twice", {"position_ticket": 7, "action": "escalate"}
    )

    assert stale == [True]
    assert len(ledger.appended) == 1
    event = ledger.appended[0]
    assert event.event_type == "guard.escalated"
    assert event.occurred_at == NOW
    assert event.payload == {
        "reason": "restore already failed twice",
        "position_ticket": 7,
        "action": "escalate",
    }


def test_the_payload_is_carried_verbatim_and_never_enriched() -> None:
    """The loop already guarantees it passes only an exception TYPE name,
    never its message, because a broker's own text -- a login, a server name
    -- can be inside one. This layer must not add anything richer than what
    it was handed: no broker read, no connection detail, no ``str(exc)``."""

    ledger = _RecordingLedger()

    LedgerEscalator(ledger, lambda: None, FixedClock(NOW)).escalate(
        "a cycle raised BrokerError", {"error_type": "BrokerError"}
    )

    assert ledger.appended[0].payload == {
        "reason": "a cycle raised BrokerError",
        "error_type": "BrokerError",
    }
