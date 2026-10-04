"""The halt ledger, and the gate every order-placing command passes.

Append-only by grant and trigger (migration 0009). A halt is in force from its
ENTERED row until a CLEARED row names it, and only a person clears one.

Entering is idempotent per switch: a second request for the same kind on the
same thing while the first is in force returns the first, and says so. That is
what keeps a guard escalating once a cycle, or a reject streak re-read on every
submission, from becoming a pile of identical halts and identical alerts. The
check and the insert run under one transaction-scoped advisory lock, so two
processes asking at once still produce one halt.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Never, Protocol
from uuid import uuid4

import psycopg

from trading_house.core.control import Halt, HaltKind, HaltRequest, HaltScope, blocking
from trading_house.core.errors import (
    DatabaseUnavailableError,
    HaltNotActiveError,
    TradingHaltedError,
)
from trading_house.execution.ledger import ConnectionFactory

CONTROL_LOCK_KEY = 20261004
"""One key for every halt write. Halts are rare and writes are tiny; a finer
key would buy concurrency nothing needs."""

_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s)"

_ACTIVE_SQL = """
SELECT e.halt_id, e.kind, e.scope, e.target, e.reason, e.actor, e.event_time
FROM execution.control_events e
WHERE e.action = 'ENTERED'
  AND NOT EXISTS (
      SELECT 1 FROM execution.control_events c
      WHERE c.halt_id = e.halt_id AND c.action = 'CLEARED'
  )
ORDER BY e.seq
"""

_INSERT_SQL = """
INSERT INTO execution.control_events
    (halt_id, action, kind, scope, target, reason, actor, event_time)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_LAST_CLEARED_SQL = """
SELECT max(event_time) FROM execution.control_events
WHERE action = 'CLEARED' AND kind = %s AND scope = %s AND target IS NOT DISTINCT FROM %s
"""


class ControlStore(Protocol):
    def enter(self, request: HaltRequest, at: datetime) -> tuple[Halt, bool]: ...
    def clear(self, halt_id: str, *, actor: str, reason: str, at: datetime) -> Halt: ...
    def active(self) -> tuple[Halt, ...]: ...
    def last_cleared(
        self, kind: HaltKind, scope: HaltScope, target: str | None
    ) -> datetime | None: ...


def _raise_database_unavailable() -> Never:
    raise DatabaseUnavailableError() from None


def _halt(row: tuple[Any, ...]) -> Halt:
    return Halt(
        halt_id=row[0],
        kind=HaltKind(row[1]),
        scope=HaltScope(row[2]),
        target=row[3],
        reason=row[4],
        actor=row[5],
        entered_at=row[6],
    )


class PostgresControlStore:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def _connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        try:
            return self._connection_factory()
        except DatabaseUnavailableError:
            raise
        except Exception:
            _raise_database_unavailable()

    def enter(self, request: HaltRequest, at: datetime) -> tuple[Halt, bool]:
        """The halt in force for this switch, and whether this call entered it."""

        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_LOCK_SQL, (CONTROL_LOCK_KEY,))
                cursor.execute(_ACTIVE_SQL)
                existing = next(
                    (halt for halt in map(_halt, cursor.fetchall()) if halt.same_switch(request)),
                    None,
                )
                if existing is not None:
                    return existing, False
                halt = Halt(halt_id=str(uuid4()), entered_at=at, **request.model_dump())
                cursor.execute(_INSERT_SQL, self._row(halt.halt_id, "ENTERED", halt, at))
        finally:
            connection.close()
        return halt, True

    def clear(self, halt_id: str, *, actor: str, reason: str, at: datetime) -> Halt:
        """Record that a person lifted the halt. Refuses one not in force."""

        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_LOCK_SQL, (CONTROL_LOCK_KEY,))
                cursor.execute(_ACTIVE_SQL)
                halt = next(
                    (halt for halt in map(_halt, cursor.fetchall()) if halt.halt_id == halt_id),
                    None,
                )
                if halt is None:
                    raise HaltNotActiveError()
                cleared = halt.model_copy(update={"reason": reason, "actor": actor})
                cursor.execute(_INSERT_SQL, self._row(halt_id, "CLEARED", cleared, at))
        except psycopg.errors.UniqueViolation:
            raise HaltNotActiveError() from None
        finally:
            connection.close()
        return halt

    def active(self) -> tuple[Halt, ...]:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_ACTIVE_SQL)
                rows = cursor.fetchall()
        finally:
            connection.close()
        return tuple(_halt(row) for row in rows)

    def last_cleared(self, kind: HaltKind, scope: HaltScope, target: str | None) -> datetime | None:
        """When a person last lifted this switch, or ``None`` if never."""

        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_LAST_CLEARED_SQL, (kind.value, scope.value, target))
                row = cursor.fetchone()
        finally:
            connection.close()
        return None if row is None else row[0]

    @staticmethod
    def _row(halt_id: str, action: str, halt: Halt, at: datetime) -> tuple[Any, ...]:
        return (
            halt_id,
            action,
            halt.kind.value,
            halt.scope.value,
            halt.target,
            halt.reason,
            halt.actor,
            at,
        )


def require_not_halted(
    store: ControlStore, *, book: str, instrument_id: str, strategy_id: str
) -> None:
    """Refuse a new order while any halt that covers it is in force."""

    halts = blocking(
        store.active(), book=book, instrument_id=instrument_id, strategy_id=strategy_id
    )
    if halts:
        raise TradingHaltedError([(halt.halt_id, halt.kind.value) for halt in halts])
