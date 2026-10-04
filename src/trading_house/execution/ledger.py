"""The append-only record of every state an order intent has passed through.

An intent is written to this ledger *before* an order is ever sent to the
broker, and ``append`` commits before it returns -- the caller's very next
action is the broker submission itself, and an uncommitted write is not a
durability point. If the process dies between an uncommitted write and the
send, a real broker position exists that nothing will ever look for, which
is the one failure this ledger exists to make impossible.

Append-only is enforced by grant and trigger (migration 0004), not by this
module's discipline: the runtime role holds only ``SELECT`` and ``INSERT``
on ``execution.intent_events``, and a trigger rejects any ``UPDATE``,
``DELETE`` or ``TRUNCATE`` regardless of who issues it. "We never call
update" is not evidence; a database that refuses the statement is.

``non_terminal`` must resolve each intent to its *latest* event, never to
any event it ever passed through -- an intent that transited SUBMITTING on
its way to CONFIRMED must not still count as open, or every successfully
completed intent would block all further trading forever.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Never, Protocol

import psycopg
from psycopg.types.json import Jsonb

from trading_house.core.errors import (
    ConcurrentSubmissionError,
    DatabaseUnavailableError,
    IntentAlreadySubmittedError,
)
from trading_house.core.values import IntentState

# One fixed key for the whole submission sequence. Per-book keys would be
# the obvious refinement, but the gate is global -- any unresolved intent
# blocks every book -- so two invocations that could safely proceed in
# parallel do not exist yet.
# ponytail: one global lock; split per book when the gate stops being global.
SUBMISSION_LOCK_KEY = 20260907

NON_TERMINAL_STATES = frozenset(
    {IntentState.SUBMITTING, IntentState.UNKNOWN, IntentState.RECONCILING}
)

_APPEND_SQL = """
INSERT INTO execution.intent_events (intent_id, state, event_time, payload)
VALUES (%s, %s, %s, %s)
"""

_CURRENT_STATE_SQL = """
SELECT state FROM execution.intent_events
WHERE intent_id = %s
ORDER BY seq DESC
LIMIT 1
"""

_EVENTS_SQL = """
SELECT seq, intent_id, state, event_time, payload
FROM execution.intent_events
WHERE intent_id = %s
ORDER BY seq
"""

_NON_TERMINAL_SQL = """
SELECT intent_id FROM (
    SELECT DISTINCT ON (intent_id) intent_id, state
    FROM execution.intent_events
    ORDER BY intent_id, seq DESC
) latest
WHERE state = ANY(%s)
"""


class ConnectionFactory(Protocol):
    """Open one distinct runtime connection for a ledger operation."""

    def __call__(self) -> psycopg.Connection[tuple[Any, ...]]: ...


class _IntentLedgerFailure(Exception):
    """Credential- and row-free diagnostic cause for a failed ledger operation."""


def _raise_database_unavailable() -> Never:
    raise DatabaseUnavailableError() from _IntentLedgerFailure("intent ledger operation failed")


def _payload_dumps(payload: Any) -> str:
    """Serialize a payload with every non-native value (notably ``Decimal``)
    turned into a string rather than silently coerced into a JSON float."""

    return json.dumps(payload, default=str)


@dataclass(frozen=True, slots=True)
class IntentEvent:
    seq: int
    intent_id: str
    state: IntentState
    event_time: datetime
    payload: Mapping[str, Any]


class IntentLedger(Protocol):
    """The surface the execution gate needs from the intent ledger."""

    def append(
        self,
        intent_id: str,
        state: IntentState,
        event_time: datetime,
        payload: Mapping[str, Any],
    ) -> None: ...

    def current_state(self, intent_id: str) -> IntentState | None: ...

    def events_for(self, intent_id: str) -> tuple[IntentEvent, ...]: ...

    def non_terminal(self) -> tuple[str, ...]: ...


def _event_from_row(row: tuple[Any, ...]) -> IntentEvent:
    return IntentEvent(
        seq=row[0],
        intent_id=row[1],
        state=IntentState(row[2]),
        event_time=row[3],
        payload=row[4],
    )


# Phase 10: the order-rate window counts SUBMITTING rows, which are written
# before the venue is called and so count every order that could have reached
# it, accepted or not.
_SUBMISSIONS_SINCE_SQL = """
SELECT payload->>'book', event_time
FROM execution.intent_events
WHERE state = 'SUBMITTING' AND event_time > %s
ORDER BY seq
"""

# Venue rejections since the last confirmation, and since a person last
# cleared safe mode (Phase 11): without the second bound a latched streak could
# never end, because no order can be confirmed while it refuses every one.
# FAILED is the reconciler's verdict on a lost order, not the broker refusing
# one, so it neither counts toward the streak nor breaks it.
_CONSECUTIVE_REJECTS_SQL = """
SELECT count(*)
FROM execution.intent_events
WHERE state = 'REJECTED'
  AND event_time > %s
  AND seq > COALESCE(
      (SELECT max(seq) FROM execution.intent_events WHERE state = 'CONFIRMED'), 0
  )
"""


class PostgresIntentLedger:
    """Append-only access to the intent ledger."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def _connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        try:
            return self._connection_factory()
        except DatabaseUnavailableError:
            raise
        except Exception:
            _raise_database_unavailable()

    def append(
        self,
        intent_id: str,
        state: IntentState,
        event_time: datetime,
        payload: Mapping[str, Any],
    ) -> None:
        """Insert one event and commit before returning.

        The commit is an explicit ``connection.commit()`` rather than a
        ``with connection:`` block left to commit implicitly on exit --
        this is the one write in the system that must be durable before
        the caller's next line runs, so it is spelled out, not implied.
        """

        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _APPEND_SQL,
                    (
                        intent_id,
                        state.value,
                        event_time,
                        Jsonb(dict(payload), dumps=_payload_dumps),
                    ),
                )
            connection.commit()
        except psycopg.errors.UniqueViolation:
            # Migration 0005: at most one SUBMITTING row per intent_id, so a
            # second submission of the same intent is refused by the database
            # rather than by a check the caller could race past.
            raise IntentAlreadySubmittedError() from None
        finally:
            connection.close()

    def current_state(self, intent_id: str) -> IntentState | None:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_CURRENT_STATE_SQL, (intent_id,))
                row = cursor.fetchone()
        finally:
            connection.close()
        return IntentState(row[0]) if row is not None else None

    def events_for(self, intent_id: str) -> tuple[IntentEvent, ...]:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_EVENTS_SQL, (intent_id,))
                rows = cursor.fetchall()
        finally:
            connection.close()
        return tuple(_event_from_row(row) for row in rows)

    def non_terminal(self) -> tuple[str, ...]:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(
                    _NON_TERMINAL_SQL,
                    ([state.value for state in NON_TERMINAL_STATES],),
                )
                rows = cursor.fetchall()
        finally:
            connection.close()
        return tuple(row[0] for row in rows)

    def submissions_since(self, start: datetime) -> tuple[tuple[str, datetime], ...]:
        """``(book, submitted_at)`` for every submission after ``start``."""

        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_SUBMISSIONS_SINCE_SQL, (start,))
                rows = cursor.fetchall()
        finally:
            connection.close()
        return tuple((row[0], row[1]) for row in rows)

    def consecutive_rejects(self, since: datetime) -> int:
        """Venue rejections after ``since`` and since the last confirmed order."""

        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_CONSECUTIVE_REJECTS_SQL, (since,))
                row = cursor.fetchone()
        finally:
            connection.close()
        return 0 if row is None else int(row[0])


@contextmanager
def submission_lock(connection_factory: ConnectionFactory) -> Iterator[None]:
    """Serialise the whole gate-then-submit sequence across processes.

    The gate reads the ledger, the submission writes SUBMITTING and commits,
    and only then does the order leave for the broker -- that commit is the
    durability point and cannot be deferred, so there is no single
    transaction spanning the sequence for ``pg_advisory_xact_lock`` to attach
    to. A session-level lock, held on a connection kept open for the
    duration, is the shape that fits: two invocations a second apart
    serialise instead of both reading a clean ledger and both sending.

    The loser refuses rather than waiting. By the time the lock frees, the
    ledger state its gate would have been deciding on is stale anyway, and
    an operator retry is a better answer than a queued order.
    """

    connection = connection_factory()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (SUBMISSION_LOCK_KEY,))
            row = cursor.fetchone()
        connection.commit()
        if row is None or not row[0]:
            raise ConcurrentSubmissionError()
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (SUBMISSION_LOCK_KEY,))
            connection.commit()
    finally:
        connection.close()
