"""Phase 11: entering and clearing halts, the audit trail, and the alert --
against an in-memory store, so the order of effects is visible."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from tests.unit.risk.conftest import BOOKS, CONFIG_DIR
from trading_house.audit.models import AuditEvent
from trading_house.constitution.loader import load_constitution
from trading_house.core.clock import FixedClock
from trading_house.core.control import SYSTEM_ACTOR, Halt, HaltKind, HaltRequest, HaltScope
from trading_house.core.errors import HaltNotActiveError
from trading_house.ops.alerts import Alert, AlertDeliveryError
from trading_house.ops.control import (
    CLEARED_EVENT,
    ENTERED_EVENT,
    UNDELIVERED_EVENT,
    ControlService,
    SafeModeEscalator,
    baselines_from,
)
from trading_house.risk.portfolio import BookPnl, PortfolioState

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
CONSTITUTION = load_constitution(
    CONFIG_DIR / "risk_constitution.yaml",
    CONFIG_DIR / "risk_constitution.yaml.sig",
    CONFIG_DIR / "risk_constitution.public.pem",
).constitution
KILL_SCALP = HaltRequest(
    kind=HaltKind.KILL, scope=HaltScope.BOOK, target="fx_scalp", reason="desk review", actor="ana"
)


@dataclass
class MemoryStore:
    """The store's contract, in memory: one halt per switch while in force,
    cleared once, and the clear times the baselines read."""

    halts: dict[str, Halt] = field(default_factory=dict)
    cleared: list[tuple[Halt, datetime]] = field(default_factory=list)

    def enter(self, request: HaltRequest, at: datetime) -> tuple[Halt, bool]:
        for halt in self.halts.values():
            if halt.same_switch(request):
                return halt, False
        halt = Halt(halt_id=str(uuid4()), entered_at=at, **request.model_dump())
        self.halts[halt.halt_id] = halt
        return halt, True

    def clear(self, halt_id: str, *, actor: str, reason: str, at: datetime) -> Halt:
        halt = self.halts.pop(halt_id, None)
        if halt is None:
            raise HaltNotActiveError()
        self.cleared.append((halt, at))
        return halt

    def active(self) -> tuple[Halt, ...]:
        return tuple(self.halts.values())

    def last_cleared(self, kind: HaltKind, scope: HaltScope, target: str | None) -> datetime | None:
        times = [
            at
            for halt, at in self.cleared
            if (halt.kind, halt.scope, halt.target) == (kind, scope, target)
        ]
        return max(times, default=None)


@dataclass
class MemoryAudit:
    events: list[AuditEvent] = field(default_factory=list)

    def append(self, event: AuditEvent) -> Any:
        self.events.append(event)
        return None

    def types(self) -> list[str]:
        return [event.event_type for event in self.events]


@dataclass
class RecordingAlerter:
    fails: bool = False
    sent: list[Alert] = field(default_factory=list)
    channel: str = "webhook:json"

    def send(self, alert: Alert) -> None:
        if self.fails:
            raise AlertDeliveryError()
        self.sent.append(alert)


def _service(
    store: MemoryStore | None = None, alerter: RecordingAlerter | None = None
) -> tuple[ControlService, MemoryStore, MemoryAudit, RecordingAlerter]:
    store = store or MemoryStore()
    audit = MemoryAudit()
    alerter = alerter or RecordingAlerter()
    return ControlService(store, audit, alerter, FixedClock(NOW)), store, audit, alerter


def test_entering_records_audits_and_alerts_once() -> None:
    service, store, audit, alerter = _service()

    outcome = service.enter(KILL_SCALP)

    assert outcome.changed
    assert outcome.alerted
    assert store.active() == (outcome.halt,)
    assert audit.types() == [ENTERED_EVENT]
    assert audit.events[0].payload["halt_id"] == outcome.halt.halt_id
    (alert,) = alerter.sent
    assert alert.severity == "critical"
    assert alert.fields["target"] == "fx_scalp"
    assert alert.fields["reason"] == "desk review"


def test_the_same_switch_again_is_the_first_halt_and_says_nothing_new() -> None:
    service, _, audit, alerter = _service()
    first = service.enter(KILL_SCALP)

    again = service.enter(KILL_SCALP.model_copy(update={"reason": "second look"}))

    assert again.halt == first.halt
    assert not again.changed
    assert not again.alerted
    assert audit.types() == [ENTERED_EVENT]
    assert len(alerter.sent) == 1


def test_a_failed_alert_is_audited_and_the_halt_still_holds() -> None:
    service, store, audit, _ = _service(alerter=RecordingAlerter(fails=True))

    outcome = service.enter(KILL_SCALP)

    assert outcome.changed
    assert not outcome.alerted
    assert store.active() == (outcome.halt,)
    assert audit.types() == [ENTERED_EVENT, UNDELIVERED_EVENT]
    assert audit.events[1].payload == {
        "halt_id": outcome.halt.halt_id,
        "event": ENTERED_EVENT,
        "channel": "webhook:json",
    }


def test_clearing_lifts_the_halt_names_who_and_tells_people() -> None:
    service, store, audit, alerter = _service()
    halt = service.enter(KILL_SCALP).halt

    outcome = service.clear(halt.halt_id, actor="ben", reason="reviewed")

    assert store.active() == ()
    assert audit.types() == [ENTERED_EVENT, CLEARED_EVENT]
    assert audit.events[1].payload["cleared_by"] == "ben"
    assert audit.events[1].payload["reason"] == "reviewed"
    assert alerter.sent[1].severity == "info"
    assert outcome.changed


def test_clearing_a_halt_not_in_force_is_refused_and_says_nothing() -> None:
    service, _, audit, alerter = _service()

    with pytest.raises(HaltNotActiveError):
        service.clear("no-such-halt", actor="ben", reason="x")

    assert audit.events == []
    assert alerter.sent == []


def test_latch_enters_what_the_portfolio_has_earned() -> None:
    service, store, _, _ = _service()
    pnl = {
        book: BookPnl(realized_today=Decimal(0), unrealized=Decimal(0), drawdown=Decimal(0))
        for book in BOOKS
    }
    pnl["fx_scalp"] = BookPnl(
        realized_today=Decimal(0), unrealized=Decimal(0), drawdown=Decimal(1800)
    )
    state = PortfolioState(
        exposures=(),
        unmeasured_positions=0,
        book_pnl=pnl,
        firm_drawdown=Decimal(0),
        order_stamps=(),
        consecutive_rejects=5,
    )

    outcomes = service.latch(CONSTITUTION, state, Decimal(100000))
    again = service.latch(CONSTITUTION, state, Decimal(100000))

    assert [o.halt.kind for o in outcomes] == [HaltKind.DRAWDOWN_HALT, HaltKind.SAFE_MODE]
    assert all(o.changed for o in outcomes)
    assert not any(o.changed for o in again)
    assert len(store.active()) == 2


def test_baselines_are_the_last_time_a_person_cleared_each_switch() -> None:
    store = MemoryStore()
    book_halt = HaltRequest(
        kind=HaltKind.DRAWDOWN_HALT,
        scope=HaltScope.BOOK,
        target="fx_swing",
        reason="book_drawdown_halt",
        actor=SYSTEM_ACTOR,
    )
    safe = HaltRequest(
        kind=HaltKind.SAFE_MODE,
        scope=HaltScope.FIRM,
        target=None,
        reason="consecutive_rejects",
        actor=SYSTEM_ACTOR,
    )
    earlier = NOW - timedelta(days=1)
    store.clear(store.enter(book_halt, earlier)[0].halt_id, actor="a", reason="r", at=earlier)
    store.clear(store.enter(book_halt, NOW)[0].halt_id, actor="a", reason="r", at=NOW)
    store.clear(store.enter(safe, NOW)[0].halt_id, actor="a", reason="r", at=NOW)

    baselines = baselines_from(store, BOOKS)

    assert baselines.book_drawdown_since == {"fx_swing": NOW}
    assert baselines.firm_drawdown_since is None
    assert baselines.rejects_since == NOW


@dataclass
class RecordingEscalator:
    calls: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    def escalate(self, reason: str, payload: Mapping[str, Any]) -> None:
        self.calls.append((reason, payload))


def test_a_guard_escalation_is_still_escalated_and_now_enters_safe_mode() -> None:
    service, store, _, alerter = _service()
    inner = RecordingEscalator()
    escalator = SafeModeEscalator(inner, service)

    escalator.escalate("stop restore failed twice", {"position_ticket": "7"})
    escalator.escalate("stop restore failed twice", {"position_ticket": "8"})

    assert inner.calls == [
        ("stop restore failed twice", {"position_ticket": "7"}),
        ("stop restore failed twice", {"position_ticket": "8"}),
    ]
    (halt,) = store.active()
    assert (halt.kind, halt.scope, halt.reason, halt.actor) == (
        HaltKind.SAFE_MODE,
        HaltScope.FIRM,
        "guard_escalation",
        SYSTEM_ACTOR,
    )
    assert len(alerter.sent) == 1
