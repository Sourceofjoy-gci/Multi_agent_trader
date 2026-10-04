"""Entering and clearing halts, and telling people about it.

``execution/control.py`` owns the halt ledger; this module joins it to the two
things it may not reach itself -- the audit chain and the alert channel -- and
to the portfolio triggers in ``risk/halts.py``.

Order of effects, on purpose: the halt row commits first, so a halt is in force
before anyone is told and whatever happens to the telling. Then the audit row,
then the alert. A failed alert is itself audited (``control.alert_undelivered``)
and never raised: the system stopping must not depend on a chat service.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from trading_house.audit.models import AuditEvent, AuditRecord
from trading_house.constitution.models import Constitution
from trading_house.core.clock import Clock
from trading_house.core.control import SYSTEM_ACTOR, Halt, HaltKind, HaltRequest, HaltScope
from trading_house.core.values import BookId
from trading_house.execution.control import ControlStore
from trading_house.ops.alerts import Alert, AlertDeliveryError, Alerter
from trading_house.ops.health import build_audit_event
from trading_house.ops.portfolio import Baselines
from trading_house.risk.halts import latch_requests
from trading_house.risk.portfolio import PortfolioState

ENTERED_EVENT = "control.halt_entered"
CLEARED_EVENT = "control.halt_cleared"
UNDELIVERED_EVENT = "control.alert_undelivered"
_SOURCE_COMPONENT = "ops.control"


class AuditAppender(Protocol):
    def append(self, event: AuditEvent) -> AuditRecord: ...


class Escalator(Protocol):
    def escalate(self, reason: str, payload: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ControlOutcome:
    halt: Halt
    changed: bool
    """True when this call entered or cleared the halt; False when an entry
    found the same switch already down."""
    alerted: bool


class ControlService:
    def __init__(
        self, store: ControlStore, audit: AuditAppender, alerter: Alerter, clock: Clock
    ) -> None:
        self._store = store
        self._audit = audit
        self._alerter = alerter
        self._clock = clock

    @property
    def channel(self) -> str:
        return self._alerter.channel

    def enter(self, request: HaltRequest) -> ControlOutcome:
        halt, entered = self._store.enter(request, self._clock.now())
        if not entered:
            # Already down: no second audit row, no second alert. The first
            # announcement is the one an operator has to act on.
            return ControlOutcome(halt=halt, changed=False, alerted=False)
        alerted = self._announce(ENTERED_EVENT, halt, "critical", "trading halted", halt.reason)
        return ControlOutcome(halt=halt, changed=True, alerted=alerted)

    def clear(self, halt_id: str, *, actor: str, reason: str) -> ControlOutcome:
        halt = self._store.clear(halt_id, actor=actor, reason=reason, at=self._clock.now())
        alerted = self._announce(
            CLEARED_EVENT, halt, "info", "halt cleared", reason, cleared_by=actor
        )
        return ControlOutcome(halt=halt, changed=True, alerted=alerted)

    def latch(
        self, constitution: Constitution, portfolio: PortfolioState, firm_equity: Decimal
    ) -> tuple[ControlOutcome, ...]:
        """Enter every halt the portfolio has already earned."""

        return tuple(
            self.enter(request) for request in latch_requests(constitution, portfolio, firm_equity)
        )

    def _announce(
        self,
        event_type: str,
        halt: Halt,
        severity: str,
        title: str,
        reason: str,
        **extra: str,
    ) -> bool:
        fields = {
            "halt_id": halt.halt_id,
            "kind": halt.kind.value,
            "scope": halt.scope.value,
            "target": halt.target or "",
            "reason": reason,
            "actor": halt.actor,
            **extra,
        }
        self._audit.append(
            build_audit_event(
                event_type,
                self._clock.now(),
                dict(fields),
                source_component=_SOURCE_COMPONENT,
            )
        )
        try:
            self._alerter.send(Alert(title=title, severity=severity, fields=fields))
        except AlertDeliveryError:
            self._audit.append(
                build_audit_event(
                    UNDELIVERED_EVENT,
                    self._clock.now(),
                    {"halt_id": halt.halt_id, "event": event_type, "channel": self.channel},
                    source_component=_SOURCE_COMPONENT,
                )
            )
            return False
        return True


def baselines_from(store: ControlStore, books: Iterable[BookId]) -> Baselines:
    book_since = {
        book: since
        for book in books
        if (since := store.last_cleared(HaltKind.DRAWDOWN_HALT, HaltScope.BOOK, book)) is not None
    }
    return Baselines(
        book_drawdown_since=book_since,
        firm_drawdown_since=store.last_cleared(HaltKind.DRAWDOWN_HALT, HaltScope.FIRM, None),
        rejects_since=store.last_cleared(HaltKind.SAFE_MODE, HaltScope.FIRM, None),
    )


class SafeModeEscalator:
    """The position guard's escalator, plus safe mode.

    Every guard escalation means the system can no longer confirm that each
    open position carries the stop it should -- master spec 13.2's own
    SAFE_MODE trigger. The guard keeps running and keeps protecting; new
    orders stop until a person has looked. The loop already reports each
    condition once per run of it and suppresses a failed report, so neither
    the halt nor its alert repeats every cycle, and neither can stop the guard.
    """

    def __init__(self, inner: Escalator, control: ControlService) -> None:
        self._inner = inner
        self._control = control

    def escalate(self, reason: str, payload: Mapping[str, Any]) -> None:
        self._inner.escalate(reason, payload)
        self._control.enter(
            HaltRequest(
                kind=HaltKind.SAFE_MODE,
                scope=HaltScope.FIRM,
                target=None,
                reason="guard_escalation",
                actor=SYSTEM_ACTOR,
            )
        )
